"""A run that dies must grade INCOMPLETE, not score its partial evidence.

Written after a drive was unplugged 1.37% into a 4 TB erase and the report
came out CAUTION -- a grade that prints, and reads like a healthy drive with
a cable quirk. erase.performed was correctly False, but only cmd_print's
INCOMPLETE check stops a report reaching paper, and CAUTION sails past it.
"""
import types

import pytest

from driveprep import grade as grading, identity as ident
from driveprep import inventory as inv, pipeline as pipe, state as st
from driveprep import supervisor as sup


class Opts:
    def __init__(self, tmp_path):
        self.output_root = str(tmp_path)
        self.seller_name = ""
        self.mask_serial = False
        self.verbose = False
        self.skip_extended_test = False
        self.stop_on_fail = False
        self.chunk_size = None


def _disk():
    """A real Disk: build_report reads far more of it than a stub carries."""
    return inv.Disk(
        kname="sdz", by_id="usb-Test_0001-0:0", synthetic_id=None,
        identity=ident.Identity(ident.CLASS_SCSI, 1 << 30, "TestModel",
                                "TESTSERIAL01"),
        bus_type="usb", size_bytes=1 << 30, logical_block_bytes=512,
        physical_block_bytes=512, sysfs_rotational=1, read_only=False,
        device_class="scsi", model="Test Model", serial="TESTSERIAL01")


def _state(tmp_path):
    return st.DriveState(
        drive_id="usb-Test_0001-0:0", output_dir=tmp_path, batch_id="B",
        run_id="R", smartctl_d_type="sat", smart_before_data=None,
        smart_available=False, capacity_bytes=1 << 30)


def _run_with(monkeypatch, tmp_path, raiser):
    """Drive run_drive through `raiser` at the erase phase."""
    monkeypatch.setattr(sup.log, "setup", lambda **k: None)
    monkeypatch.setattr(sup.log, "add_run_log", lambda p: None)
    monkeypatch.setattr(sup, "_install_child_signals", lambda: None)
    monkeypatch.setattr(pipe.DrivePipeline, "phase4_erase", raiser)
    monkeypatch.setattr(pipe.DrivePipeline, "phase5_verify",
                        lambda self: None)
    monkeypatch.setattr(pipe.DrivePipeline, "phase6_extended_test",
                        lambda self: None)
    monkeypatch.setattr(pipe.DrivePipeline, "phase7_smart_after",
                        lambda self: None)

    captured = {}
    monkeypatch.setattr(pipe.DrivePipeline, "emit",
                        lambda self, report: captured.update(report))

    state = _state(tmp_path)
    sup.run_drive(_disk(), state, {"io": {}, "checkpoint": {}},
                  Opts(tmp_path))
    return captured


def test_an_unhandled_error_mid_erase_grades_incomplete(monkeypatch, tmp_path):
    """The regression: the drive was yanked and the report said CAUTION."""
    def boom(self):
        raise OSError(5, "Input/output error")

    report = _run_with(monkeypatch, tmp_path, boom)

    assert report["flags"]["interrupted"] is True
    assert report["grade"]["value"] == grading.INCOMPLETE, \
        "a run that died must never be scored on the evidence it did collect"
    assert report["erase"]["performed"] is False


def test_the_incomplete_reason_names_the_actual_error(monkeypatch, tmp_path):
    def boom(self):
        raise OSError(5, "Input/output error")

    report = _run_with(monkeypatch, tmp_path, boom)
    assert any("OSError" in r for r in report["grade"]["reasons"]), \
        report["grade"]["reasons"]


def test_a_device_that_never_came_back_grades_incomplete(monkeypatch,
                                                         tmp_path):
    """DriveAborted that is neither thermal nor a disconnect count.

    "device did not return within N s" set no flag at all: not
    too_many_disconnects (the word 'disconnected' is absent) and not
    thermally_aborted. Grading saw an unremarkable run.
    """
    def gone(self):
        raise pipe.DriveAborted("device did not return within 120 s")

    report = _run_with(monkeypatch, tmp_path, gone)

    assert report["grade"]["value"] == grading.INCOMPLETE
    assert any("did not return" in r for r in report["grade"]["reasons"]), \
        report["grade"]["reasons"]


def test_too_many_disconnects_keeps_its_own_reason(monkeypatch, tmp_path):
    """The pre-existing path must not gain a duplicate generic sentence."""
    def gone(self):
        raise pipe.DriveAborted("device disconnected 6 times in one phase")

    report = _run_with(monkeypatch, tmp_path, gone)

    assert report["grade"]["value"] == grading.INCOMPLETE
    assert report["flags"]["too_many_disconnects"] is True
    assert report["flags"]["interrupted"] is False, \
        "the disconnect flag already covers this; do not double-report it"


def test_a_signal_still_grades_incomplete(monkeypatch, tmp_path):
    """The path that already worked, guarded against regression."""
    def stopped(self):
        raise pipe.DriveInterrupted("interrupted by signal")

    report = _run_with(monkeypatch, tmp_path, stopped)

    assert report["flags"]["interrupted"] is True
    assert report["grade"]["value"] == grading.INCOMPLETE


@pytest.mark.parametrize("raiser", [
    pipe.DriveInterrupted("interrupted by signal"),
    pipe.DriveAborted("device did not return within 60 s"),
    OSError(5, "Input/output error"),
])
def test_an_unfinished_drive_can_still_be_resumed(monkeypatch, tmp_path,
                                                  raiser):
    """Ctrl+C at 40% used to leave a drive `resume` called "nothing to resume".

    Building the INCOMPLETE report advanced the checkpoint to the report
    phase, which find_resumable skips.
    """
    def stop_mid_erase(self):
        self.state.enter_phase(st.PHASE_ERASE)
        self.state.phase_offset = 123 * 1024 * 1024
        self.state.checkpoint(force=True)
        raise raiser

    report = _run_with(monkeypatch, tmp_path, stop_mid_erase)
    assert report["grade"]["value"] == grading.INCOMPLETE

    saved = st.DriveState.load(tmp_path)
    assert saved.phase == st.PHASE_ERASE
    assert saved.phase_offset == 123 * 1024 * 1024
    assert tmp_path in st.find_resumable(tmp_path.parent)


def test_an_abandoned_erase_grades_fail_and_skips_the_rest(monkeypatch,
                                                           tmp_path):
    """Write failures used to raise, and the drive came out INCOMPLETE."""
    from driveprep import blockio

    def failing_fill(self, write):
        findings = self.state.erase_findings
        findings.record_write_error(blockio.Range(1 << 20, 1 << 20, 512), 100)
        findings.write_abandoned = True
        findings.bytes_done = 2 << 20

    monkeypatch.setattr(pipe.DrivePipeline, "_run_pass", failing_fill)
    monkeypatch.setattr(pipe.safety, "reread_partition_table", lambda d: None)
    state = _state(tmp_path)
    p = pipe.DrivePipeline(_disk(), state, grading.load_config(),
                           Opts(tmp_path))
    p.phase4_erase()
    p.phase5_verify()
    p.phase6_extended_test()
    report = p.build_report()

    assert report["grade"]["value"] == grading.FAIL
    assert report["erase"]["performed"] is False
    assert report["erase"]["bytes_written"] == 2 << 20
    assert report["verify"]["performed"] is False
    assert report["self_tests"]["extended"]["status"] == "skipped_already_failed"
