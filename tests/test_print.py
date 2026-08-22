"""Two-page print bundle and the print subcommand."""

from __future__ import annotations

import copy

import pytest

from driveprep import grade as grading, report as reporting


def _bundle(report, tmp_path):
    path = tmp_path / "report-print.html"
    reporting.render_print_bundle(report, path)
    return path, path.read_text(encoding="utf-8")


def test_bundle_has_exactly_two_sheets(clean_report, tmp_path):
    _p, html = _bundle(clean_report, tmp_path)
    assert html.count('class="sheet') == 2


def test_bundle_is_self_contained(clean_report, tmp_path):
    """Same rule as report.html: nothing may be fetched at open time."""
    _p, html = _bundle(clean_report, tmp_path)
    lowered = html.lower()
    for forbidden in ("http://", "https://", "//cdn", "@import", "<script",
                      '<link'):
        assert forbidden not in lowered, forbidden


def test_bundle_page_one_is_the_report(clean_report, tmp_path):
    _p, html = _bundle(clean_report, tmp_path)
    assert "PASS" in html
    assert "read back as zero" in html
    assert "SMART attributes" in html.replace("SMART ATTRIBUTES", "SMART attributes")


def test_bundle_page_two_explains_the_blank_drive(clean_report, tmp_path):
    """The buyer's first reaction is 'it doesn't work' -- answer that first."""
    _p, html = _bundle(clean_report, tmp_path)
    assert "blank on purpose" in html
    assert "is not a fault" in html
    for os_name in ("Windows", "macOS", "Linux"):
        assert os_name in html
    for fs in ("exFAT", "NTFS", "APFS", "ext4"):
        assert fs in html


def test_bundle_page_two_carries_the_drive_identity(clean_report, tmp_path):
    """Page two must be about THIS drive, not a generic leaflet."""
    report = copy.deepcopy(clean_report)
    report["drive"]["model"] = "WDC WD5000AAVS-00ZTB0"
    report["drive"]["capacity_label"] = "500 GB"
    _p, html = _bundle(report, tmp_path)
    assert "WDC WD5000AAVS-00ZTB0" in html
    assert "500 GB" in html
    assert report["report_id"] in html


def test_bundle_page_two_makes_no_warranty_claim(clean_report, tmp_path):
    _p, html = _bundle(clean_report, tmp_path)
    assert "not a third-party certification" in html
    assert "not a warranty" in html


def test_bundle_names_the_seller_when_given(clean_report, tmp_path):
    report = copy.deepcopy(clean_report)
    report["seller_name"] = "Dave's Drives"
    _p, html = _bundle(report, tmp_path)
    assert "Sold by Dave&#x27;s Drives." in html or "Sold by Dave's Drives." in html

    report["seller_name"] = ""
    _p, html = _bundle(report, tmp_path)
    assert "Sold by" not in html


def test_stylesheet_is_shared_not_duplicated(clean_report, tmp_path):
    """One stylesheet for report.html and the bundle, so they cannot drift."""
    _p, bundle = _bundle(clean_report, tmp_path)
    reporting.render_html(clean_report, tmp_path / "report.html")
    report_html = (tmp_path / "report.html").read_text(encoding="utf-8")

    assert bundle.count("<style>") == 1
    marker = ".badge-word {"
    assert marker in bundle and marker in report_html


def test_bundle_declares_a_single_page_box_per_sheet(clean_report, tmp_path):
    """Without this the fixed 1200x1600 design slices across three pages."""
    _p, html = _bundle(clean_report, tmp_path)
    assert "@page" in html
    assert "12.5in 16.67in" in html
    assert "print-color-adjust: exact" in html
    assert "page-break-after: always" in html


def test_pdf_page_count_parses_the_page_tree(tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n1 0 obj<</Type /Pages /Count 2 /Kids[]>>endobj\n")
    assert reporting.pdf_page_count(pdf) == 2
    assert reporting.pdf_page_count(tmp_path / "missing.pdf") == 0


def test_missing_browser_does_not_fail_the_pdf_path(clean_report, tmp_path,
                                                    monkeypatch):
    monkeypatch.setattr(reporting, "find_chrome", lambda: (None, False))
    path, _html = _bundle(clean_report, tmp_path)
    assert reporting.render_pdf(path, tmp_path / "report.pdf") is False
    assert path.exists(), "the printable HTML must survive for manual printing"


def test_incomplete_runs_are_not_printed(clean_report, tmp_path, config,
                                         monkeypatch, capsys):
    """A report that says DO NOT USE IN A LISTING must not be handed over.

    Printing it produces a physical page that outlives the warning on screen.
    """
    from driveprep.__main__ import cmd_print
    import json

    directory = tmp_path / "drive-x"
    directory.mkdir()
    report = copy.deepcopy(clean_report)
    report["flags"]["thermally_aborted"] = True
    report["grade"] = grading.evaluate(report, config).to_json()
    assert report["grade"]["value"] == grading.INCOMPLETE
    (directory / "report.json").write_text(json.dumps(report))

    monkeypatch.setattr(reporting, "render_pdf", lambda *a, **k: True)
    monkeypatch.setattr(reporting, "pdf_page_count", lambda *a, **k: 2)
    (directory / "report.pdf").write_bytes(b"%PDF-1.4\n")

    class Opts:
        output_root = str(tmp_path)
        ids = []
        all_drives = True
        printer = "fake"
        copies = 1
        dry_run = False

    assert cmd_print(Opts()) == 1
    out = capsys.readouterr().out
    assert "SKIPPED" in out and "INCOMPLETE" in out


def test_print_reports_when_no_printer_is_configured(clean_report, tmp_path,
                                                     monkeypatch, capsys):
    from driveprep.__main__ import cmd_print
    import json

    directory = tmp_path / "drive-y"
    directory.mkdir()
    (directory / "report.json").write_text(json.dumps(clean_report))
    monkeypatch.setattr(reporting, "render_pdf", lambda *a, **k: True)
    monkeypatch.setattr(reporting, "pdf_page_count", lambda *a, **k: 2)

    class Opts:
        output_root = str(tmp_path)
        ids = []
        all_drives = True
        printer = None
        copies = 1
        dry_run = False

    monkeypatch.setattr("driveprep.__main__._default_printer", lambda: None)
    cmd_print(Opts())
    out = capsys.readouterr().out
    assert "No printer configured" in out
    assert "lpadmin" in out, "tell the operator how to fix it"


def _print_opts(tmp_path, ids, **kw):
    class Opts:
        output_root = str(tmp_path)
        all_drives = False
        printer = "fake"
        copies = 1
        dry_run = True
    Opts.ids = ids
    for key, value in kw.items():
        setattr(Opts, key, value)
    return Opts()


def _stored_run(tmp_path, dir_name, clean_report):
    import json
    directory = tmp_path / dir_name
    directory.mkdir()
    (directory / "report.json").write_text(json.dumps(clean_report))
    return directory


def test_print_id_accepts_the_by_id_spelling(clean_report, tmp_path,
                                             monkeypatch, capsys):
    """--id must take the name `driveprep list` prints, colons and all.

    The output directory replaces colons with underscores, so pasting the
    by-id name used to match nothing and report "Nothing to print" -- with
    the drive's report sitting right there on disk.
    """
    from driveprep.__main__ import cmd_print

    _stored_run(tmp_path, "usb-WDC_WD40-0_0", clean_report)
    monkeypatch.setattr(reporting, "render_pdf", lambda *a, **k: True)
    monkeypatch.setattr(reporting, "pdf_page_count", lambda *a, **k: 2)

    assert cmd_print(_print_opts(tmp_path, ["usb-WDC_WD40-0:0"])) == 0
    out = capsys.readouterr().out
    assert "Nothing to print" not in out
    assert "No stored run matches" not in out


def test_print_id_still_accepts_the_directory_spelling(clean_report, tmp_path,
                                                       monkeypatch, capsys):
    """The underscored form kept working; this fix must not trade one for
    the other."""
    from driveprep.__main__ import cmd_print

    _stored_run(tmp_path, "usb-WDC_WD40-0_0", clean_report)
    monkeypatch.setattr(reporting, "render_pdf", lambda *a, **k: True)
    monkeypatch.setattr(reporting, "pdf_page_count", lambda *a, **k: 2)

    assert cmd_print(_print_opts(tmp_path, ["usb-WDC_WD40-0_0"])) == 0
    assert "No stored run matches" not in capsys.readouterr().out


def test_print_names_an_id_that_matches_nothing(clean_report, tmp_path,
                                                capsys):
    """A typo must say so, and say what IS stored.

    Silence was the actual bug: an unmatched --id read exactly like an empty
    output root, so the advice was to rebuild reports that already existed.
    """
    from driveprep.__main__ import cmd_print

    _stored_run(tmp_path, "usb-WDC_WD40-0_0", clean_report)

    assert cmd_print(_print_opts(tmp_path, ["usb-TYPO-0:0"])) == 1
    out = capsys.readouterr().out
    assert "No stored run matches" in out
    assert "usb-TYPO-0_0" in out
    assert "usb-WDC_WD40-0_0" in out, "show the operator what is available"
    assert "Run `driveprep report --all` first" not in out, (
        "that advice is for an empty output root, not a bad --id")


def test_print_warns_about_a_bad_id_but_still_prints_the_good_one(
        clean_report, tmp_path, monkeypatch, capsys):
    """One bad --id out of two must not silently swallow the good one."""
    from driveprep.__main__ import cmd_print

    _stored_run(tmp_path, "usb-WDC_WD40-0_0", clean_report)
    monkeypatch.setattr(reporting, "render_pdf", lambda *a, **k: True)
    monkeypatch.setattr(reporting, "pdf_page_count", lambda *a, **k: 2)

    opts = _print_opts(tmp_path, ["usb-WDC_WD40-0:0", "usb-GONE-0:1"])
    assert cmd_print(opts) == 0
    out = capsys.readouterr().out
    assert "No stored run matches" in out and "usb-GONE-0_1" in out


def test_sanitize_id_is_the_one_definition():
    """output_name and --id matching must not drift apart."""
    from driveprep import inventory as inv

    raw = "usb-Test_Drive_0001_ENCL0000000-0:1"
    assert inv.sanitize_id(raw) == "usb-Test_Drive_0001_ENCL0000000-0_1"
    assert inv.sanitize_id(inv.sanitize_id(raw)) == inv.sanitize_id(raw), (
        "must be idempotent, or the directory spelling would not match itself")
