"""`driveprep recheck`: correcting a self-test the drive finished later.

This command rewrites a health result, so its constraints matter more than its
happy path. It exists because an extended self-test can outlive the tool's
polling patience -- observed on two real drives that were declared
"inconclusive" 20 minutes into six-hour tests they went on to complete.
"""

from __future__ import annotations

import copy
import json

import pytest

from driveprep import grade as grading, report as reporting
from driveprep.__main__ import cmd_recheck


class Opts:
    def __init__(self, root):
        self.output_root = str(root)
        self.ids = []
        self.all_drives = True
        self.seller_name = ""
        self.mask_serial = False


def _drive(tmp_path, clean_report, extended_status, name="drive-a"):
    directory = tmp_path / name
    directory.mkdir()
    report = copy.deepcopy(clean_report)
    report["drive"]["by_id"] = name
    report["drive"]["smartctl_device_type"] = "sat"
    report["self_tests"]["extended"] = {"run": True, "status": extended_status,
                                        "duration_s": 1200,
                                        "lba_of_first_error": None}
    report["grade"] = grading.evaluate(report).to_json()
    (directory / "report.json").write_text(json.dumps(report))
    return directory


def _entry(kind="Extended offline", passed=True, string="Completed without error",
           lba=None, lifetime=9999):
    return {"type": {"string": kind},
            "status": {"string": string, "passed": passed},
            "lifetime_hours": lifetime,
            "lba": lba}


@pytest.fixture(autouse=True)
def _no_rendering(monkeypatch):
    monkeypatch.setattr(reporting, "render_html", lambda r, p: p)
    monkeypatch.setattr(reporting, "render_png", lambda *a: True)
    monkeypatch.setattr(reporting, "render_print_bundle", lambda r, p: p)
    monkeypatch.setattr(reporting, "render_pdf", lambda *a: True)


def _attach(monkeypatch, name, entry):
    from driveprep import __main__ as cli, smart
    disk = type("D", (), {"id": name, "dev_path": f"/dev/{name}"})()
    monkeypatch.setattr(cli.inv, "scan", lambda: [disk])
    monkeypatch.setattr(smart, "last_selftest_entry", lambda *a, **k: entry)


def test_a_completed_test_replaces_an_inconclusive_record(tmp_path,
                                                          clean_report,
                                                          monkeypatch, capsys):
    directory = _drive(tmp_path, clean_report, "inconclusive")
    _attach(monkeypatch, "drive-a", _entry())

    assert cmd_recheck(Opts(tmp_path)) == 0
    data = json.loads((directory / "report.json").read_text())
    assert data["self_tests"]["extended"]["status"] == "completed_without_error"
    assert data["self_tests"]["extended"]["rechecked"] is True
    assert data["grade"]["value"] == grading.PASS, "the CAUTION reason is gone"


def test_a_failed_test_is_written_exactly_as_read(tmp_path, clean_report,
                                                  monkeypatch):
    """This must be able to make a report worse, not only better."""
    directory = _drive(tmp_path, clean_report, "inconclusive")
    _attach(monkeypatch, "drive-a",
            _entry(passed=False, string="Completed: read failure", lba=884736))

    cmd_recheck(Opts(tmp_path))
    data = json.loads((directory / "report.json").read_text())
    assert data["self_tests"]["extended"]["status"] == "completed:_read_failure"
    assert data["self_tests"]["extended"]["lba_of_first_error"] == 884736
    assert data["grade"]["value"] == grading.FAIL


@pytest.mark.parametrize("status", ["completed_without_error", "read_failure",
                                    "servo_failure"])
def test_an_already_decided_result_is_never_overwritten(tmp_path, clean_report,
                                                        monkeypatch, status):
    """Only inconclusive/interrupted may be corrected."""
    directory = _drive(tmp_path, clean_report, status)
    before = (directory / "report.json").read_text()
    _attach(monkeypatch, "drive-a", _entry())

    cmd_recheck(Opts(tmp_path))
    assert (directory / "report.json").read_text() == before, \
        f"a recorded {status!r} must not be rewritten"


def test_a_short_test_log_entry_is_not_mistaken_for_the_extended_one(
        tmp_path, clean_report, monkeypatch):
    """The newest entry is often the short test; it must not be promoted."""
    directory = _drive(tmp_path, clean_report, "inconclusive")
    before = (directory / "report.json").read_text()
    _attach(monkeypatch, "drive-a", _entry(kind="Short offline"))

    cmd_recheck(Opts(tmp_path))
    assert (directory / "report.json").read_text() == before


def test_an_entry_without_a_verdict_is_ignored(tmp_path, clean_report,
                                               monkeypatch):
    directory = _drive(tmp_path, clean_report, "inconclusive")
    before = (directory / "report.json").read_text()
    _attach(monkeypatch, "drive-a", _entry(passed=None))

    cmd_recheck(Opts(tmp_path))
    assert (directory / "report.json").read_text() == before


def test_a_detached_drive_is_skipped(tmp_path, clean_report, monkeypatch,
                                     capsys):
    """Nothing may be inferred about a drive that is not present."""
    directory = _drive(tmp_path, clean_report, "inconclusive")
    before = (directory / "report.json").read_text()
    from driveprep import __main__ as cli
    monkeypatch.setattr(cli.inv, "scan", lambda: [])

    cmd_recheck(Opts(tmp_path))
    assert (directory / "report.json").read_text() == before
    assert "not currently attached" in capsys.readouterr().out


def test_recheck_never_opens_a_device_for_writing(tmp_path, clean_report,
                                                  monkeypatch):
    """It reads a log; it must never touch the block device."""
    import os
    opened = []
    real_open = os.open

    def spy(path, flags, *a, **k):
        opened.append((str(path), flags))
        return real_open(path, flags, *a, **k)

    _drive(tmp_path, clean_report, "inconclusive")
    _attach(monkeypatch, "drive-a", _entry())
    monkeypatch.setattr(os, "open", spy)
    cmd_recheck(Opts(tmp_path))

    writable = [(p, f) for p, f in opened
                if p.startswith("/dev/") and (f & os.O_RDWR or f & os.O_WRONLY)]
    assert writable == [], f"recheck opened a device for writing: {writable}"


def test_a_rechecked_result_does_not_keep_the_observed_duration(tmp_path,
                                                                clean_report,
                                                                monkeypatch):
    """1200s measured how long the tool watched, not how long the drive ran.

    Printing it as the test duration on a buyer-facing page is false. The
    drive's log carries the outcome but not the elapsed time, so the honest
    answer is that it was not measured.
    """
    directory = _drive(tmp_path, clean_report, "inconclusive")
    _attach(monkeypatch, "drive-a", _entry())

    cmd_recheck(Opts(tmp_path))
    extended = json.loads((directory / "report.json").read_text())["self_tests"]["extended"]
    assert extended["status"] == "completed_without_error"
    assert extended["duration_s"] is None, \
        "an unmeasured duration must not be reported as measured"


def test_the_report_renders_an_unmeasured_duration_honestly(tmp_path,
                                                            clean_report):
    """It must not render as a number, and must not claim zero."""
    report = copy.deepcopy(clean_report)
    report["self_tests"]["extended"] = {
        "run": True, "status": "completed_without_error",
        "duration_s": None, "lba_of_first_error": None, "rechecked": True}
    html = reporting._selftest_kv(report)
    assert "completed without error" in html
    extended = html.split("Extended self-test duration")[1]
    assert "20 min" not in extended
    assert "min" not in extended.split("</div>")[0], \
        "an unmeasured duration must not render as a time"
    assert "read from the drive" in extended


# --------------------------------------------------------------------------
# The entry must belong to THIS run
# --------------------------------------------------------------------------


def test_a_selftest_predating_the_erase_is_refused(tmp_path, clean_report,
                                                   monkeypatch, capsys):
    """Otherwise a report claims a surface test the drive passed in a past life.

    Found on a real drive whose log held an extended test from exactly the hour
    the run ended -- ambiguous whether it ran before or after that erase.
    """
    directory = _drive(tmp_path, clean_report, "inconclusive")
    before = (directory / "report.json").read_text()
    # clean_report records power_on_hours = 1200
    _attach(monkeypatch, "drive-a", _entry(lifetime=1100))

    cmd_recheck(Opts(tmp_path))
    assert (directory / "report.json").read_text() == before
    assert "predates this erase" in capsys.readouterr().out


def test_a_selftest_from_the_same_hour_is_refused_as_ambiguous(tmp_path,
                                                               clean_report,
                                                               monkeypatch):
    directory = _drive(tmp_path, clean_report, "inconclusive")
    before = (directory / "report.json").read_text()
    _attach(monkeypatch, "drive-a", _entry(lifetime=1200))   # == run hours

    cmd_recheck(Opts(tmp_path))
    assert (directory / "report.json").read_text() == before, \
        "same-hour is not provably after the erase"


def test_a_selftest_after_the_run_is_accepted(tmp_path, clean_report,
                                              monkeypatch):
    directory = _drive(tmp_path, clean_report, "inconclusive")
    _attach(monkeypatch, "drive-a", _entry(lifetime=1201))

    cmd_recheck(Opts(tmp_path))
    data = json.loads((directory / "report.json").read_text())
    assert data["self_tests"]["extended"]["status"] == "completed_without_error"


def test_an_undateable_entry_is_refused(tmp_path, clean_report, monkeypatch,
                                        capsys):
    directory = _drive(tmp_path, clean_report, "inconclusive")
    before = (directory / "report.json").read_text()
    entry = _entry()
    del entry["lifetime_hours"]
    _attach(monkeypatch, "drive-a", entry)

    cmd_recheck(Opts(tmp_path))
    assert (directory / "report.json").read_text() == before
    assert "cannot date" in capsys.readouterr().out


def test_a_skipped_test_can_be_filled_in_later(tmp_path, clean_report,
                                               monkeypatch):
    """--skip-extended-test records an absence, not a verdict.

    An operator who skips it and later runs the test by hand should get the
    real outcome rather than a permanent CAUTION.
    """
    directory = _drive(tmp_path, clean_report, "skipped")
    _attach(monkeypatch, "drive-a", _entry(lifetime=1300))

    cmd_recheck(Opts(tmp_path))
    data = json.loads((directory / "report.json").read_text())
    assert data["self_tests"]["extended"]["status"] == "completed_without_error"
    assert data["grade"]["value"] == grading.PASS


def test_a_recorded_verdict_is_still_never_overwritten(tmp_path, clean_report,
                                                       monkeypatch):
    """Widening to 'skipped' must not have widened to real results."""
    for status in ("completed_without_error", "read_failure"):
        directory = _drive(tmp_path, clean_report, status, name=f"d-{status}")
        before = (directory / "report.json").read_text()
        _attach(monkeypatch, f"d-{status}", _entry(lifetime=99999))
        cmd_recheck(Opts(tmp_path))
        assert (directory / "report.json").read_text() == before, status
