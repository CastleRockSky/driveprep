"""Report rendering: spec 14 tests 12 and 16."""

from __future__ import annotations

import copy
import re
import shutil
import struct

import pytest

from driveprep import grade as grading, report as reporting


def _render(report, tmp_path, name="report.html"):
    path = tmp_path / name
    reporting.render_html(report, path)
    return path, path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# HTML content
# --------------------------------------------------------------------------


def test_html_is_self_contained(clean_report, tmp_path):
    """No external fetches of any kind: no CDN, no web fonts, no remote images."""
    _path, html = _render(clean_report, tmp_path)
    lowered = html.lower()
    for forbidden in ("http://", "https://", "//cdn", "@import", "<script"):
        assert forbidden not in lowered, f"found {forbidden!r} in the report"


def test_html_shows_the_grade_word_not_just_a_colour(clean_report, tmp_path):
    """The word carries the meaning so it survives greyscale and CVD."""
    _path, html = _render(clean_report, tmp_path)
    assert "PASS" in html
    assert reporting.GRADE_GLYPHS[grading.PASS] in html


def test_html_reports_capacity_the_way_a_seller_advertises_it(clean_report,
                                                              tmp_path):
    report = copy.deepcopy(clean_report)
    report["drive"]["capacity_label"] = "4 TB"
    _path, html = _render(report, tmp_path)
    assert "4 TB" in html


def test_html_shows_every_headline_tile(clean_report, tmp_path):
    _path, html = _render(clean_report, tmp_path)
    for label in ("Power-on hours", "Power cycles", "Reallocated sectors",
                  "Pending sectors"):
        assert label in html, label
    assert "1,200" in html   # power-on hours, thousands-separated
    assert "40" in html      # power cycles


def test_html_states_the_verification_outcome_as_a_sentence(clean_report,
                                                            tmp_path):
    _path, html = _render(clean_report, tmp_path)
    assert "read back as zero" in html
    assert "0 read errors" in html


def test_read_errors_do_not_read_as_a_successful_verification(clean_report,
                                                              tmp_path):
    """"covered N bytes and found 7 read errors" implied N bytes were verified.

    They were not: the regions that errored could not be read back at all, so
    the erase is unproven there and the report must say so.
    """
    report = copy.deepcopy(clean_report)
    report["verify"] = {
        **clean_report["verify"], "read_errors": 7,
        "read_error_ranges": [
            {"start_byte": 4096, "length_bytes": 4096, "first_lba": 8,
             "last_lba": 15},
            {"start_byte": 999424, "length_bytes": 8192, "first_lba": 1952,
             "last_lba": 1967},
        ],
    }
    _path, html = _render(report, tmp_path)
    assert "could not be verified across those regions" in html
    assert "covered" not in html.split("Erase and verification")[1][:400]
    assert "7 read error(s) across 2 region(s)" in html
    assert "first at LBA 8" in html


def test_nonzero_regions_report_where_they_start(clean_report, tmp_path):
    """Clustered damage and scattered damage are different products."""
    report = copy.deepcopy(clean_report)
    report["verify"] = {
        **clean_report["verify"],
        "nonzero_ranges": [{"start_byte": 512, "length_bytes": 512,
                            "first_lba": 1, "last_lba": 1}],
    }
    _path, html = _render(report, tmp_path)
    assert "did not read back as zero" in html
    assert "first at LBA 1" in html


def test_clean_verification_keeps_the_plain_all_zero_sentence(clean_report,
                                                              tmp_path):
    _path, html = _render(clean_report, tmp_path)
    assert "read back as zero, with 0 read errors" in html
    assert "could not be verified" not in html


def test_methodology_does_not_contradict_a_failed_verification(clean_report,
                                                               tmp_path):
    """The footer claimed every sector was confirmed while the body said not.

    A methodology footer that contradicts its own report is worse than none.
    """
    report = copy.deepcopy(clean_report)
    report["verify"] = {**clean_report["verify"], "read_errors": 7,
                        "read_error_ranges": [{"first_lba": 8}]}
    _path, html = _render(report, tmp_path)
    assert "could not be confirmed" in html
    assert "read back to confirm the erase completed" not in html
    # The NIST alignment still holds -- the write happened, the proof did not.
    assert "NIST SP 800-88" in html


def test_methodology_keeps_the_full_claim_on_a_clean_verification(clean_report,
                                                                  tmp_path):
    _path, html = _render(clean_report, tmp_path)
    assert "read back to confirm the erase completed" in html
    assert "could not be confirmed" not in html


def test_nonzero_regions_also_soften_the_methodology(clean_report, tmp_path):
    report = copy.deepcopy(clean_report)
    report["verify"] = {**clean_report["verify"],
                        "nonzero_ranges": [{"first_lba": 3}]}
    _path, html = _render(report, tmp_path)
    assert "could not be confirmed" in html


def test_first_lba_helper_is_robust_to_missing_keys():
    assert reporting._first_lba([]) == ""
    assert reporting._first_lba(None) == ""
    assert reporting._first_lba([{"start_byte": 0}]) == ""
    assert reporting._first_lba(
        [{"first_lba": 90}, {"first_lba": 12}]) == ", first at LBA 12"


def test_html_never_claims_certification(clean_report, tmp_path):
    """Report wording must not imply a NAID-certified attestation."""
    _path, html = _render(clean_report, tmp_path)
    assert "not a third-party" in html
    assert "certificate of destruction" in html.lower()
    assert "NIST SP 800-88" in html


def test_unrun_self_tests_say_why_they_did_not_run(clean_report, tmp_path):
    """The pipeline records the reason; the report must not discard it.

    A bare "not run" leaves a buyer asking why the drive was not tested, when
    the answer is already known.
    """
    report = copy.deepcopy(clean_report)
    report["self_tests"] = {
        "short": {"run": False, "status": "smart_unavailable"},
        "extended": {"run": False, "status": "skipped"},
    }
    _path, html = _render(report, tmp_path)
    assert "SMART unavailable through this bridge" in html
    assert "skipped with --skip-extended-test" in html
    # The heading is uppercased by CSS, so the source reads "Self-tests".
    selftest_block = html.split("Self-tests")[1].split("</section>")[0]
    assert "unknown" not in selftest_block, \
        "an unrun test must not render a duration"
    assert "duration" not in selftest_block.lower()


def test_unknown_not_run_status_falls_back_cleanly(clean_report, tmp_path):
    report = copy.deepcopy(clean_report)
    report["self_tests"] = {
        "short": {"run": False, "status": "something_new"},
        "extended": {"run": False, "status": ""},
    }
    _path, html = _render(report, tmp_path)
    assert "not run" in html


def test_smart_unavailable_renders_a_sentence_not_a_blank_table(clean_report,
                                                                tmp_path):
    report = copy.deepcopy(clean_report)
    report["smart"] = {"available": False, "overall_health": None,
                       "power_on_hours": None, "power_cycles": None,
                       "attributes": []}
    _path, html = _render(report, tmp_path)
    assert "not available through this drive" in html
    assert "not available" in html  # tiles too
    assert "SATA" in html


# --------------------------------------------------------------------------
# Test 16: erase.performed == false
# --------------------------------------------------------------------------


def test_16_not_erased_notice_is_rendered_prominently(clean_report, tmp_path,
                                                      config):
    """A drive that failed the health gate was never written to.

    A report that implies a drive was sanitized when it was not is the single
    worst output this tool can produce.
    """
    report = copy.deepcopy(clean_report)
    report["erase"] = {
        "performed": False,
        "not_performed_reason": "the short self-test reported a read failure",
        "method": None, "bytes_written": None, "started_utc": None,
        "finished_utc": None, "duration_s": None,
    }
    report["verify"] = {"performed": False, "bytes_read": None,
                        "nonzero_ranges": [], "read_error_ranges": [],
                        "read_errors": None, "duration_s": None}
    report["self_tests"]["short"] = {"run": True, "status": "read_failure",
                                     "duration_s": 94,
                                     "lba_of_first_error": 884736}
    report["grade"] = grading.evaluate(report, config).to_json()

    _path, html = _render(report, tmp_path)
    assert "NOT ERASED" in html
    assert "Any previous data is still present" in html
    assert "not-erased" in html, "must use the high-contrast notice styling"
    assert "read back as zero" not in html, \
        "must not render a verification claim for a drive that was not erased"
    assert report["grade"]["value"] == grading.FAIL


def test_16_normal_report_does_not_show_the_not_erased_notice(clean_report,
                                                              tmp_path):
    _path, html = _render(clean_report, tmp_path)
    # Match the rendered element, not the bare phrase: the stylesheet comments
    # legitimately mention the notice by name.
    assert ">NOT ERASED<" not in html


# --------------------------------------------------------------------------
# INCOMPLETE must be marked unusable (spec 10.2)
# --------------------------------------------------------------------------


def test_incomplete_report_says_do_not_use_in_a_listing(clean_report, tmp_path,
                                                        config):
    """Spec 10.2 requires this, and it is the most important line on the page.

    An incomplete run can still carry a complete, truthful erase block -- a
    thermal abort during the phase-6 self-test happens after erase and verify
    have both finished -- so without the banner the page reads as an ordinary
    successful run wearing an odd grey badge.
    """
    report = copy.deepcopy(clean_report)
    report["flags"]["thermally_aborted"] = True
    report["run_conditions"]["thermal_abort_reason"] = (
        "temperature stayed at or above 60 C for more than 5 minutes")
    report["grade"] = grading.evaluate(report, config).to_json()
    assert report["grade"]["value"] == grading.INCOMPLETE

    _path, html = _render(report, tmp_path)
    assert "DO NOT USE IN A LISTING" in html
    assert "RUN DID NOT COMPLETE" in html
    assert "incomplete-notice" in html
    assert "temperature stayed at or above 60 C" in html, \
        "the full reason must appear, not just the badge's ellipsised copy"


def test_incomplete_banner_appears_even_when_the_erase_completed(clean_report,
                                                                 tmp_path,
                                                                 config):
    """The dangerous case: a truthful erase block under an unfinished run."""
    report = copy.deepcopy(clean_report)
    report["flags"]["interrupted"] = True
    report["grade"] = grading.evaluate(report, config).to_json()

    _path, html = _render(report, tmp_path)
    assert report["erase"]["performed"] is True
    assert "read back as zero" in html, "the erase block is still truthful"
    assert "DO NOT USE IN A LISTING" in html, \
        "but the report must still be marked unusable"


@pytest.mark.parametrize("value", ["PASS", "CAUTION", "FAIL"])
def test_completed_runs_never_show_the_incomplete_banner(clean_report,
                                                         tmp_path, value):
    report = copy.deepcopy(clean_report)
    report["grade"] = {"value": value, "reasons": [], "rubric_version": 1}
    _path, html = _render(report, tmp_path)
    assert "DO NOT USE IN A LISTING" not in html


def test_incomplete_badge_carries_no_pass_caution_fail_word(clean_report,
                                                            tmp_path, config):
    report = copy.deepcopy(clean_report)
    report["flags"]["thermally_aborted"] = True
    report["grade"] = grading.evaluate(report, config).to_json()
    _path, html = _render(report, tmp_path)
    # Extract the badge <section> properly; splitting on 'class="badge' also
    # matches badge-glyph/badge-word and truncates the segment.
    badge = re.search(r'<section class="badge .*?</section>', html,
                      re.DOTALL).group(0)
    assert ">INCOMPLETE<" in badge
    for word in ("PASS", "CAUTION", "FAIL"):
        assert f">{word}<" not in badge


def test_16_methodology_omits_the_sanitization_claim_when_not_erased(
        clean_report, tmp_path):
    report = copy.deepcopy(clean_report)
    report["erase"] = {"performed": False, "not_performed_reason": "x"}
    report["verify"] = {"performed": False, "nonzero_ranges": [],
                        "read_error_ranges": []}
    _path, html = _render(report, tmp_path)
    assert "No erase was performed" in html
    assert "renders data unrecoverable" not in html


# --------------------------------------------------------------------------
# SMART table sizing (spec 11.2)
# --------------------------------------------------------------------------


def _many_attrs(report, count):
    report = copy.deepcopy(report)
    report["smart"]["attributes"] = [
        {"id": i, "name": f"Attr_{i}", "value": 100, "worst": 100,
         "thresh": 0, "raw": 0, "flags": "--", "when_failed": ""}
        for i in range(1, count + 1)
    ]
    return report


def test_table_drops_a_type_size_before_it_truncates(clean_report):
    count = reporting.ROWS_NORMAL + 3
    rows, density, truncated = reporting._smart_rows(
        _many_attrs(clean_report, count))
    assert density == "dense"
    assert not truncated
    assert rows.count("<tr") == count


def test_table_stays_at_normal_density_up_to_its_capacity(clean_report):
    rows, density, truncated = reporting._smart_rows(
        _many_attrs(clean_report, reporting.ROWS_NORMAL))
    assert density == ""
    assert not truncated
    assert rows.count("<tr") == reporting.ROWS_NORMAL


def test_emitted_rows_never_exceed_the_measured_page_capacity(clean_report):
    """The truncation note states a number, so emitted rows must all fit.

    If Python emits more rows than the flex region holds, CSS clips the excess
    and the note undercounts what is hidden -- the page then contradicts
    itself. These constants are measured against the rendered 1200x1600 page;
    this pins them so a later tweak cannot silently break the arithmetic.
    """
    for count in (5, reporting.ROWS_NORMAL, reporting.ROWS_DENSE, 45, 120):
        rows, _density, truncated = reporting._smart_rows(
            _many_attrs(clean_report, count))
        emitted = rows.count("<tr")
        assert emitted <= reporting.ROWS_DENSE
        if truncated:
            hidden = int(re.search(r">(\d+) further", truncated).group(1))
            assert emitted + hidden == count, \
                f"{count} attributes: {emitted} shown + {hidden} claimed hidden"


def test_table_truncates_visibly_rather_than_growing_the_page(clean_report):
    rows, density, truncated = reporting._smart_rows(
        _many_attrs(clean_report, 45))
    assert density == "dense"
    assert rows.count("<tr") == reporting.ROWS_DENSE
    assert "further attribute" in truncated
    assert "report.json" in truncated
    assert f"{45 - reporting.ROWS_DENSE} further" in truncated


def test_badge_caps_reasons_and_always_shows_the_overflow_counter(clean_report,
                                                                  tmp_path):
    """A drive with more reasons than fit must not look like it had only those.

    The badge clips at a fixed height, so an unbounded reason list silently
    dropped entries with no indication any existed.
    """
    report = copy.deepcopy(clean_report)
    report["grade"] = {"value": "CAUTION", "rubric_version": 1,
                       "reasons": [f"CAUTION: reason number {i}"
                                   for i in range(1, 7)]}
    _path, html = _render(report, tmp_path)
    assert html.count("<li>") == reporting.BADGE_REASONS
    assert f"+{6 - reporting.BADGE_REASONS} more reasons" in html
    assert "reason number 1" in html
    assert "reason number 6" not in html


def test_badge_omits_the_counter_when_everything_fits(clean_report, tmp_path):
    report = copy.deepcopy(clean_report)
    report["grade"] = {"value": "CAUTION", "rubric_version": 1,
                       "reasons": ["CAUTION: only one thing"]}
    _path, html = _render(report, tmp_path)
    assert "more reason" not in html


def test_power_on_hours_tile_explains_a_vendor_encoded_raw(clean_report,
                                                           tmp_path):
    """The tile shows decoded hours, the table shows attribute 9's raw.

    On firmware that encodes attribute 9 as minutes -- or packs counters into
    the 48-bit field -- the two legitimately disagree, and a reader comparing
    them sees a self-contradictory page unless it says which is which.
    """
    report = copy.deepcopy(clean_report)
    report["smart"]["power_on_hours"] = 14203
    report["smart"]["attributes"] = [
        {"id": 9, "name": "Power_On_Hours", "value": 81, "worst": 81,
         "thresh": 0, "raw": 852180, "flags": "-O--CK", "when_failed": ""},
    ]
    _path, html = _render(report, tmp_path)
    assert "14,203" in html, "the tile shows the decoded value"
    assert "852,180" in html, "the raw is still printed"
    assert "attribute 9 raw reads 852,180" in html


def test_power_on_hours_tile_is_unannotated_when_they_agree(clean_report,
                                                            tmp_path):
    report = copy.deepcopy(clean_report)
    report["smart"]["power_on_hours"] = 1200
    report["smart"]["attributes"] = [
        {"id": 9, "name": "Power_On_Hours", "value": 81, "worst": 81,
         "thresh": 0, "raw": 1200, "flags": "-O--CK", "when_failed": ""},
    ]
    _path, html = _render(report, tmp_path)
    assert "attribute 9 raw reads" not in html


def test_missing_attribute_is_not_reported_as_a_blocked_bridge(clean_report,
                                                               tmp_path):
    """Two different reasons a tile is empty; conflating them is a false claim.

    SMART readable but the drive does not report attribute 5 is NOT the same
    as SMART blocked by the bridge, and saying the latter would be wrong.
    """
    report = copy.deepcopy(clean_report)
    report["smart"]["attributes"] = []          # SMART fine, attribute absent
    _path, html = _render(report, tmp_path)
    assert "not reported by this drive" in html
    assert "SMART blocked by bridge" not in html

    report["smart"]["available"] = False
    _path, html = _render(report, tmp_path)
    assert "SMART blocked by bridge" in html


def test_truncation_never_hides_an_attribute_that_fed_the_grade(clean_report):
    """Spec 10: every raw number behind the grade must be printed.

    Head-truncation would drop a flagged attribute that happens to sort late,
    hiding the exact number that justified the drive's grade.
    """
    report = copy.deepcopy(clean_report)
    # 40 ordinary attributes at low IDs, with the rubric-relevant ones placed
    # last so a naive attributes[:limit] would cut them all.
    ordinary = [{"id": i, "name": f"Ordinary_{i}", "value": 100, "worst": 100,
                 "thresh": 0, "raw": 0, "flags": "--", "when_failed": ""}
                for i in range(20, 60)]
    critical = [
        {"id": 197, "name": "Current_Pending_Sector", "value": 180,
         "worst": 180, "thresh": 0, "raw": 24, "flags": "PO--CK",
         "when_failed": "now"},
        {"id": 5, "name": "Reallocated_Sector_Ct", "value": 140, "worst": 140,
         "thresh": 140, "raw": 96, "flags": "PO--CK", "when_failed": ""},
        {"id": 199, "name": "UDMA_CRC_Error_Count", "value": 200, "worst": 200,
         "thresh": 0, "raw": 3, "flags": "PO--CK", "when_failed": ""},
    ]
    report["smart"]["attributes"] = ordinary + critical

    rows, _density, truncated = reporting._smart_rows(report)
    assert truncated, "this fixture must truncate for the test to mean anything"
    for name, raw in (("Current_Pending_Sector", "24"),
                      ("Reallocated_Sector_Ct", "96"),
                      ("UDMA_CRC_Error_Count", "3")):
        assert name in rows, f"{name} fed the grade and must be printed"
        assert f">{raw}<" in rows


def test_retained_rows_stay_in_the_drives_own_order(clean_report):
    """The table is a reference table, not a ranked list."""
    report = copy.deepcopy(clean_report)
    ordinary = [{"id": i, "name": f"Ordinary_{i}", "value": 100, "worst": 100,
                 "thresh": 0, "raw": 0, "flags": "--", "when_failed": ""}
                for i in range(20, 60)]
    report["smart"]["attributes"] = ordinary + [
        {"id": 197, "name": "Current_Pending_Sector", "value": 180,
         "worst": 180, "thresh": 0, "raw": 24, "flags": "PO--CK",
         "when_failed": "now"}]

    rows, _density, _truncated = reporting._smart_rows(report)
    ids = [int(m) for m in re.findall(r'<td class="l">(\d+)</td>', rows)]
    assert ids == sorted(ids), "rows must remain in the drive's reported order"
    assert 197 in ids


def test_selection_is_a_noop_when_everything_fits(clean_report):
    attrs = [{"id": i, "name": f"A_{i}", "value": 1, "worst": 1, "thresh": 0,
              "raw": 0, "flags": "", "when_failed": ""} for i in range(1, 10)]
    assert reporting._select_rows(attrs, 26) == attrs


def test_flagged_attributes_are_marked_in_the_table(clean_report):
    report = copy.deepcopy(clean_report)
    report["smart"]["attributes"] = [
        {"id": 197, "name": "Current_Pending_Sector", "value": 180,
         "worst": 180, "thresh": 0, "raw": 24, "flags": "PO--CK",
         "when_failed": "now"},
    ]
    rows, _density, _truncated = reporting._smart_rows(report)
    assert 'class="flagged"' in rows
    assert "24" in rows


# --------------------------------------------------------------------------
# Test 12: the PNG
# --------------------------------------------------------------------------


def test_12_png_dimensions_are_read_from_the_ihdr(tmp_path):
    """No image library: the IHDR chunk is at a fixed offset."""
    fake = tmp_path / "fake.png"
    fake.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
        + struct.pack(">II", 1200, 1600) + b"\x08\x06\x00\x00\x00"
    )
    assert reporting.png_dimensions(fake) == (1200, 1600)

    not_a_png = tmp_path / "x.png"
    not_a_png.write_bytes(b"nope")
    assert reporting.png_dimensions(not_a_png) is None


def test_12_missing_browser_never_fails_the_run(clean_report, tmp_path,
                                                monkeypatch):
    """Never fail a 20-hour run over a missing browser."""
    monkeypatch.setattr(reporting, "find_chrome", lambda: (None, False))
    html_path, _html = _render(clean_report, tmp_path)
    assert reporting.render_png(html_path, tmp_path / "report.png") is False
    assert html_path.exists(), "the HTML must survive for manual screenshotting"


def test_12_snap_without_sudo_user_is_refused_not_silently_broken(
        clean_report, tmp_path, monkeypatch):
    """A snap-confinement file:// failure produces a zero-byte PNG otherwise."""
    monkeypatch.setattr(reporting, "find_chrome",
                        lambda: ("/snap/bin/chromium", True))
    monkeypatch.setattr(reporting, "_invoking_user_home", lambda: None)
    html_path, _html = _render(clean_report, tmp_path)
    assert reporting.render_png(html_path, tmp_path / "report.png") is False


@pytest.mark.skipif(shutil.which("google-chrome-stable") is None
                    and shutil.which("chromium") is None
                    and shutil.which("chromium-browser") is None,
                    reason="no Chrome/Chromium available to render a PNG")
def test_12_png_renders_at_exactly_1200x1600(clean_report, tmp_path):
    html_path, _html = _render(clean_report, tmp_path)
    png_path = tmp_path / "report.png"
    assert reporting.render_png(html_path, png_path) is True
    assert png_path.stat().st_size > reporting.MIN_PNG_BYTES
    assert reporting.png_dimensions(png_path) == (1200, 1600)


# --------------------------------------------------------------------------
# Batch index
# --------------------------------------------------------------------------


def test_batch_index_lists_every_drive_and_grade(tmp_path):
    summary = {
        "batch_id": "B-20260802-0258",
        "generated_utc": "2026-08-02T03:00:00Z",
        "drives": [
            {"drive_id": "usb-A-0:0", "output_name": "usb-A-0_0",
             "model": "WD40EZRZ", "capacity_label": "4 TB",
             "grade": "PASS", "reasons": []},
            {"drive_id": "usb-B-0:0", "output_name": "usb-B-0_0",
             "model": "ST2000DM", "capacity_label": "2 TB",
             "grade": "FAIL", "reasons": ["FAIL: 3 read error(s)"]},
        ],
    }
    reporting.render_batch_index(summary, tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "usb-A-0:0" in html and "usb-B-0:0" in html
    assert "PASS" in html and "FAIL" in html
    assert "3 read error(s)" in html
    assert (tmp_path / "index.json").exists()


def test_caution_is_visually_calmer_than_fail(clean_report, tmp_path):
    """CAUTION must not wear FAIL's alarm styling.

    Most honest used drives land on CAUTION -- one reallocated sector, a CRC
    error, high hours, a skipped extended test. Dressing that like FAIL both
    overstates it and makes a genuinely bad drive harder to spot, which is the
    more dangerous error.
    """
    css = (reporting.TEMPLATE_DIR / "report.css").read_text(encoding="utf-8")
    caution = css.split(".badge.caution {")[1].split("}")[0]
    assert "background: var(--page)" in caution, \
        "CAUTION sits on the plain page surface, not a tinted alarm fill"
    assert "border-left: 14px solid var(--warn)" in caution, \
        "colour survives as an edge accent"
    # FAIL keeps the full treatment.
    assert "background: var(--crit-tint)" in css


def test_caution_keeps_the_word_and_glyph_at_full_size(clean_report, tmp_path):
    """Toning down must not cost thumbnail legibility or greyscale survival."""
    css = (reporting.TEMPLATE_DIR / "report.css").read_text(encoding="utf-8")
    assert ".badge-word {" in css
    word_rule = css.split(".badge-word {")[1].split("}")[0]
    assert "66px" in word_rule, "the grade must stay readable at 400px wide"

    report = copy.deepcopy(clean_report)
    report["grade"] = {"value": "CAUTION", "rubric_version": 1,
                       "reasons": ["CAUTION: extended self-test was skipped"]}
    _path, html = _render(report, tmp_path)
    assert ">CAUTION<" in html
    assert reporting.GRADE_GLYPHS[grading.CAUTION] in html
    assert "extended self-test was skipped" in html, "information is retained"


def test_caution_glyph_colour_is_contrast_safe_on_white(clean_report):
    """#fab219 measures 1.83:1 on white -- invisible. The ink token is darker."""
    css = (reporting.TEMPLATE_DIR / "report.css").read_text(encoding="utf-8")
    assert "--warn-ink:    #a27410" in css

    def _lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    def _lum(hexval):
        h = hexval.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)

    ratio = (_lum("#ffffff") + 0.05) / (_lum("#a27410") + 0.05)
    assert ratio >= 3.0, f"large-text contrast floor is 3:1, got {ratio:.2f}"


def test_limitations_only_caution_is_framed_as_information(clean_report,
                                                           tmp_path, config):
    """A bridge that will not report a self-test is not a drive fault."""
    report = copy.deepcopy(clean_report)
    report["drive"]["bus_type"] = "usb"
    report["flags"]["skipped_extended_test"] = True
    report["grade"] = grading.evaluate(report, config).to_json()
    assert report["grade"]["limitations_only"] is True

    _path, html = _render(report, tmp_path)
    assert "Nothing was found wrong with this drive" in html
    assert "could <b>not</b> be measured, not faults" in html
    assert "USB enclosure" in html, "explain the bridge limitation"
    assert "directly to SATA" in html, "and what would fix it"
    assert 'class="badge caution informational"' in html
    assert ">CAUTION<" in html, "the grade word itself is unchanged"


def test_limitations_note_omits_the_usb_line_on_sata(clean_report, tmp_path,
                                                     config):
    report = copy.deepcopy(clean_report)
    report["drive"]["bus_type"] = "ata"
    report["flags"]["skipped_extended_test"] = True
    report["grade"] = grading.evaluate(report, config).to_json()
    _path, html = _render(report, tmp_path)
    assert "Nothing was found wrong" in html
    assert "USB enclosure" not in html, "no bridge to blame on a SATA drive"


def test_a_measured_finding_keeps_the_warning_styling(clean_report, tmp_path,
                                                      config):
    """The informational treatment must not leak onto a real concern."""
    report = copy.deepcopy(clean_report)
    report["smart"]["attributes"] = [
        {"id": 5, "name": "Reallocated_Sector_Ct", "value": 140, "worst": 140,
         "thresh": 140, "raw": 8, "flags": "PO--CK", "when_failed": ""}]
    report["flags"]["skipped_extended_test"] = True
    report["grade"] = grading.evaluate(report, config).to_json()

    _path, html = _render(report, tmp_path)
    assert "informational" not in html.split("<style>")[1].split("</style>")[1]
    assert "Nothing was found wrong" not in html
    assert "Reallocated Sector Count" in html


# --------------------------------------------------------------------------
# Vendor-encoded raw values
# --------------------------------------------------------------------------


def _attrs(pairs):
    return [{"id": i, "name": n, "value": 100, "worst": 100, "thresh": 0,
             "raw": r, "flags": "", "when_failed": ""} for i, n, r in pairs]


def test_seagate_ratio_attributes_are_annotated(clean_report):
    """"Raw_Read_Error_Rate = 41,452,976" is not 41 million read errors.

    Seagate encodes errors-against-total-operations there. Printed bare, it
    reads as a dying drive to anyone who does not know the encoding.
    """
    report = copy.deepcopy(clean_report)
    report["drive"]["model"] = "ST1000DM003-1SB102"
    report["smart"]["attributes"] = _attrs([
        (1, "Raw_Read_Error_Rate", 41452976),
        (7, "Seek_Error_Rate", 2431542),
        (195, "Hardware_ECC_Recovered", 41452976)])
    rows, _d, note = reporting._smart_rows(report)
    assert rows.count("†") == 3
    assert "Vendor-encoded raw" in note
    assert "does not indicate errors" in note


def test_packed_temperature_is_annotated_on_any_vendor(clean_report):
    report = copy.deepcopy(clean_report)
    report["drive"]["model"] = "WDC WD40EZRZ"
    report["smart"]["attributes"] = _attrs([
        (194, "Temperature_Celsius", 64424509467),
        (190, "Airflow_Temperature_Cel", 705888283)])
    rows, _d, note = reporting._smart_rows(report)
    assert rows.count("†") == 2, "packed fields are not Seagate-specific"
    assert "Vendor-encoded raw" in note


def test_a_plain_temperature_is_not_annotated(clean_report):
    """The annotation must appear only where it is actually needed."""
    report = copy.deepcopy(clean_report)
    report["smart"]["attributes"] = _attrs([(194, "Temperature_Celsius", 38)])
    rows, _d, note = reporting._smart_rows(report)
    assert "†" not in rows
    assert "Vendor-encoded" not in note


def test_genuine_counts_are_never_annotated(clean_report):
    """Total LBAs Written/Read are real and useful; do not undermine them."""
    report = copy.deepcopy(clean_report)
    report["drive"]["model"] = "ST4000DM000-1F2168"
    report["smart"]["attributes"] = _attrs([
        (241, "Total_LBAs_Written", 2596720136),
        (242, "Total_LBAs_Read", 2031236656),
        (5, "Reallocated_Sector_Ct", 0)])
    rows, _d, note = reporting._smart_rows(report)
    assert "†" not in rows
    assert "Vendor-encoded" not in note


def test_ratio_attributes_are_not_annotated_on_a_non_seagate(clean_report):
    """Attribute 1 on a WD drive is a plain count; leave it alone."""
    report = copy.deepcopy(clean_report)
    report["drive"]["model"] = "WDC WD40EZRZ-00GXCB0"
    report["smart"]["attributes"] = _attrs([(1, "Raw_Read_Error_Rate", 41452976)])
    rows, _d, note = reporting._smart_rows(report)
    assert "†" not in rows


def test_annotated_attributes_still_show_their_real_raw(clean_report):
    """Annotate, never hide -- the number stays printed in full."""
    report = copy.deepcopy(clean_report)
    report["drive"]["model"] = "ST4000DM000-1F2168"
    report["smart"]["attributes"] = _attrs([(1, "Raw_Read_Error_Rate", 41452976)])
    rows, _d, _n = reporting._smart_rows(report)
    assert "41,452,976" in rows
