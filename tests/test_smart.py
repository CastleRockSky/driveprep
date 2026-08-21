"""smartctl parsing and probe acceptance: spec 14 test 11.

Fixtures are captured --json output shapes from two real drives: one
USB-bridged and one SATA.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from driveprep import kernlog, smart, identity as ident

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def bridged():
    return _load("smart-usb-bridged.json")


@pytest.fixture
def sata():
    return _load("smart-sata.json")


# --------------------------------------------------------------------------
# Test 11: probe acceptance
# --------------------------------------------------------------------------


def test_11_bridged_drive_with_a_different_model_is_accepted(bridged):
    """The single most important acceptance rule (spec 6.1).

    sysfs reports the bridge's SCSI INQUIRY model ('Elements 25A2', capped at
    16 characters); ATA IDENTIFY through the bridge returns the drive's own
    model ('WD40EZRZ-00GXCB0'). A model-equality check would reject every
    bridged drive and route them all to 'SMART unavailable'.
    """
    sysfs_model = "Elements 25A2"
    sysfs_size = 4000787030016
    assert bridged["model_name"] != sysfs_model

    ok, reason = smart._plausible(bridged, sysfs_size)
    assert ok, reason
    assert "capacity" in reason


def test_11_serial_differing_between_enclosure_and_drive_is_fine(bridged):
    enclosure_serial = "575830303031"
    assert bridged["serial_number"] != enclosure_serial
    assert smart._plausible(bridged, 4000787030016)[0]


def test_11_sata_drive_is_accepted(sata):
    assert smart._plausible(sata, sata["user_capacity"]["bytes"])[0]


def test_11_fabricated_capacity_is_rejected(bridged):
    """sat,12 frequently returns structurally valid but invented IDENTIFY."""
    fabricated = {**bridged, "user_capacity": {"bytes": 64424509440}}
    ok, reason = smart._plausible(fabricated, 4000787030016)
    assert not ok
    assert "capacity" in reason


def test_11_zero_capacity_is_rejected(bridged):
    ok, reason = smart._plausible({**bridged, "user_capacity": {"bytes": 0}},
                                  4000787030016)
    assert not ok and "zero" in reason


@pytest.mark.parametrize("field", ["model_name", "firmware_version",
                                   "serial_number"])
def test_11_implausible_identify_is_rejected(bridged, field):
    ok, reason = smart._plausible({**bridged, field: ""}, 4000787030016)
    assert not ok
    assert "implausible" in reason


def test_11_capacity_within_one_percent_is_accepted(bridged):
    sysfs = 4000787030016
    near = {**bridged, "user_capacity": {"bytes": int(sysfs * 0.995)}}
    assert smart._plausible(near, sysfs)[0]
    far = {**bridged, "user_capacity": {"bytes": int(sysfs * 0.97)}}
    assert not smart._plausible(far, sysfs)[0]


# --------------------------------------------------------------------------
# Test 11: decoding
# --------------------------------------------------------------------------


def test_11_attributes_flatten_raw_and_keep_when_failed_a_string(bridged):
    result = smart.SmartResult(available=True, d_type="sat", data=bridged)
    attr = result.attribute(5)
    assert attr is not None
    assert isinstance(attr["raw"], int), "raw is smartctl's nested raw.value"
    assert attr["when_failed"] == "", "when_failed is a string, never None"
    assert isinstance(attr["flags"], str)


def test_11_power_on_hours_uses_the_decoded_field(bridged):
    result = smart.SmartResult(available=True, d_type="sat", data=bridged)
    assert result.power_on_hours == bridged["power_on_time"]["hours"]


def test_11_implausible_power_on_hours_becomes_unknown(bridged):
    data = {**bridged, "power_on_time": {"hours": 999999}}
    assert smart.SmartResult(True, "sat", data).power_on_hours is None


def test_11_power_cycles_and_temperature_decode(bridged):
    result = smart.SmartResult(available=True, d_type="sat", data=bridged)
    assert result.power_cycles == bridged["power_cycle_count"]
    assert result.temperature_c == bridged["temperature"]["current"]


def test_11_overall_health_maps_to_passed_failed(bridged):
    assert smart.SmartResult(True, "sat", bridged).overall_health == "PASSED"
    failed = {**bridged, "smart_status": {"passed": False}}
    assert smart.SmartResult(True, "sat", failed).overall_health == "FAILED"
    assert smart.SmartResult(True, "sat", {}).overall_health is None


def test_11_rotation_rate_is_passed_through_verbatim(bridged, sata):
    assert smart.SmartResult(True, "sat", bridged).rotation_rate == 5400
    assert smart.SmartResult(True, None, sata).rotation_rate == 7200
    assert smart.SmartResult(True, "sat", {}).rotation_rate is None, \
        "absent means unknown, which is eligible"


def test_11_delta_reports_only_changed_attributes(bridged):
    before = smart.SmartResult(True, "sat", bridged)
    after_data = json.loads(json.dumps(bridged))
    for row in after_data["ata_smart_attributes"]["table"]:
        if row["id"] == 194:
            row["raw"]["value"] = 44
    after = smart.SmartResult(True, "sat", after_data)
    changed = smart.delta(before, after)
    assert len(changed) == 1
    assert changed[0]["id"] == 194
    assert changed[0]["after"] == 44


def test_11_selftest_polling_estimates_are_read(bridged):
    poll = smart.SmartResult(True, "sat", bridged).selftest_polling_minutes
    assert poll["short"] == 2
    assert poll["extended"] == 494


def test_11_probe_log_entries_serialize():
    attempt = smart.ProbeAttempt("sat,12", False, "capacity mismatch")
    assert attempt.to_json() == {"d_type": "sat,12", "accepted": False,
                                 "reason": "capacity mismatch"}
    assert smart.ProbeAttempt(None, True, "ok").to_json()["d_type"] == "(none)"


def test_11_selftest_result_serializes():
    result = smart.SelfTestResult(run=True, status="completed_without_error",
                                  duration_s=118)
    assert result.to_json()["status"] == "completed_without_error"
    assert not result.fatal
    assert smart.SelfTestResult(True, "read failure").fatal


# --------------------------------------------------------------------------
# Kernel log classification (spec 6.4)
# --------------------------------------------------------------------------


EPOCH = ident.LocatorEpoch(hctl="6:0:0:0", usb_port_path="2-1.4",
                           kernel_name="sdc", valid_from="2026-08-02T03:00:00Z")


@pytest.mark.parametrize("line,category", [
    ("blk_update_request: I/O error, dev sdc, sector 12345", "io_errors"),
    ("Buffer I/O error on dev sdc1, logical block 9, async page read",
     "io_errors"),
    ("sd 6:0:0:0: [sdc] tag#0 FAILED Result: critical medium error",
     "medium_errors"),
    ("usb 2-1.4: reset high-speed USB device number 5", "usb_resets"),
    ("sd 6:0:0:0: [sdc] uas_eh_abort_handler 0 uas-tag 1", "uas_aborts"),
])
def test_kernel_lines_match_their_epoch_and_classify(line, category):
    assert kernlog._line_matches_epoch(line, EPOCH), line
    assert kernlog._classify(line) == category


@pytest.mark.parametrize("line", [
    "blk_update_request: I/O error, dev sdd, sector 99",
    "blk_update_request: I/O error, dev sdca, sector 99",
    "sd 9:0:0:0: [sdd] Attached SCSI disk",
])
def test_other_devices_do_not_match_this_epoch(line):
    assert not kernlog._line_matches_epoch(line, EPOCH), line


def test_kernel_events_split_drive_faults_from_link_faults():
    events = kernlog.KernelEvents()
    events.counts["usb_resets"] = 3
    assert events.has_link_fault and not events.has_drive_fault
    events.counts["io_errors"] = 1
    assert events.has_drive_fault


# --------------------------------------------------------------------------
# Self-test polling estimate must survive the parent/child boundary
# --------------------------------------------------------------------------


def test_polling_estimate_falls_back_to_the_persisted_snapshot(tmp_path,
                                                               bridged):
    """Phase 1 runs in the parent; the self-tests run in the child.

    With self.smart_before None in the child, the estimate fell back to None,
    run_selftest() defaulted to 5 minutes, and the no-progress deadline became
    15 minutes. A six-hour extended test does not move remaining_percent in 15
    minutes, so a healthy drive was declared "inconclusive" 20 minutes in and
    graded CAUTION. Observed on two real drives that were still running their
    tests at the time.
    """
    from driveprep import pipeline as pipe, state as st

    class Opts:
        chunk_size = None
        test_mode = True
        output_root = str(tmp_path)
        seller_name = ""
        mask_serial = False
        verbose = False
        skip_extended_test = False

    state = st.DriveState(drive_id="d", output_dir=tmp_path, batch_id="B",
                          run_id="R", smartctl_d_type="sat",
                          smart_before_data=bridged)
    disk = type("D", (), {"logical_block_bytes": 512,
                          "physical_block_bytes": 512, "id": "d",
                          "size_bytes": 1 << 30})()
    p = pipe.DrivePipeline(disk, state, {"io": {}, "checkpoint": {}}, Opts())

    assert p.smart_before is None, "the child never runs phase 1 itself"
    assert p._polling_estimate("extended") == 494, \
        "the estimate must come from the persisted snapshot"
    assert p._polling_estimate("short") == 2


def test_polling_estimate_prefers_a_live_snapshot_when_present(tmp_path,
                                                               bridged):
    from driveprep import pipeline as pipe, smart, state as st

    class Opts:
        chunk_size = None
        test_mode = True
        output_root = str(tmp_path)
        seller_name = ""
        mask_serial = False
        verbose = False
        skip_extended_test = False

    state = st.DriveState(drive_id="d", output_dir=tmp_path, batch_id="B",
                          run_id="R", smart_before_data=None)
    disk = type("D", (), {"logical_block_bytes": 512,
                          "physical_block_bytes": 512, "id": "d",
                          "size_bytes": 1 << 30})()
    p = pipe.DrivePipeline(disk, state, {"io": {}, "checkpoint": {}}, Opts())
    p.smart_before = smart.SmartResult(available=True, d_type="sat",
                                       data=bridged)
    assert p._polling_estimate("extended") == 494


def test_a_realistic_estimate_gives_a_deadline_longer_than_the_test(bridged):
    """The deadline must exceed the drive's own estimate, not undercut it."""
    from driveprep import smart as S
    minutes = S.SmartResult(True, "sat", bridged).selftest_polling_minutes
    deadline_s = minutes["extended"] * 60 * 3.0     # no_progress_factor
    assert deadline_s > minutes["extended"] * 60, \
        "a stall deadline shorter than the test itself fails healthy drives"
    # The broken fallback produced 900s against a 29,640s test.
    assert deadline_s > 900 * 10


# --------------------------------------------------------------------------
# Kernel-log evidence: two defects that made every count structurally zero
# --------------------------------------------------------------------------


@pytest.mark.parametrize("line", [
    "usb 2-2: reset SuperSpeed USB device number 11 using xhci_hcd",
    "usb 2-2: reset SuperSpeed Plus USB device number 3 using xhci_hcd",
    "usb 2-1: reset high-speed USB device number 5 using xhci_hcd",
    "usb 1-7: reset full-speed USB device number 9 using xhci_hcd",
])
def test_every_link_speed_spelling_counts_as_a_reset(line):
    """The kernel writes 'SuperSpeed' as one word, unlike 'high-speed'.

    The pattern required a separator -- (high|full|low|super)[- ]speed -- so it
    matched USB 2 resets and silently ignored every USB 3 one. The only test
    covering it used 'high-speed', which is hyphenated, so it passed while
    USB 3 evidence was invisible. A dock that reset 127 times in an hour
    reported usb_resets=0.
    """
    assert kernlog._classify(line) == "usb_resets", line


def test_journal_window_is_timezone_independent():
    """Epoch stamps are UTC; journalctl reads bare timestamps as LOCAL.

    Dropping the Z produced a wall-clock string that journalctl interpreted in
    the host's zone, shifting every scan window by the UTC offset -- six hours
    on America/Denver. A run whose window genuinely contained a USB disconnect
    scanned six hours of unrelated log and reported nothing.
    """
    rendered = kernlog._iso_for_journal("2026-08-19T18:17:29Z")
    assert rendered.startswith("@"), \
        "must use the @<epoch> form, which systemd always reads as UTC"
    assert int(rendered[1:]) == 1787163449

    # Same instant written three ways must produce the same query.
    for equivalent in ("2026-08-19T18:17:29+00:00", "2026-08-19T12:17:29-06:00"):
        assert kernlog._iso_for_journal(equivalent) == rendered, equivalent


def test_journal_window_never_silently_reinterprets_a_local_stamp():
    """A naive stamp is assumed UTC, matching how epochs are recorded."""
    assert kernlog._iso_for_journal("2026-08-19T18:17:29") == \
        kernlog._iso_for_journal("2026-08-19T18:17:29Z")


def test_unparseable_stamp_falls_back_rather_than_inventing_a_window():
    assert kernlog._iso_for_journal("not a timestamp") == "not a timestamp"


def test_the_disconnect_that_ends_an_epoch_is_classified_and_matched():
    """End to end on the real line, against the real epoch it fell inside.

    The WD Elements vanished mid-erase at 18:33:36Z and returned two seconds
    later. Both halves have to work for that to reach the report: the line must
    match the epoch's locator, and it must classify as a link fault.
    """
    epoch = ident.LocatorEpoch(hctl="1:0:0:0", usb_port_path="2-2",
                               kernel_name="sdb",
                               valid_from="2026-08-19T18:17:29Z",
                               valid_until="2026-08-19T18:33:36Z")
    line = "2026-08-19T18:33:36+0000 host kernel: usb 2-2: USB disconnect, device number 11"
    assert kernlog._line_matches_epoch(line, epoch)
    assert kernlog._classify(line) == "usb_resets"


def test_the_epoch_closing_event_is_inside_the_queried_window():
    """journalctl's --until is EXCLUSIVE at one-second granularity.

    An epoch's valid_until is the instant its locator changed -- normally the
    USB disconnect itself. Querying that boundary verbatim drops exactly the
    event that closed the epoch, which is the one most worth reporting.
    Verified against the real journal: --until at the disconnect's own second
    returned nothing, one second later returned the line.
    """
    stamp = kernlog._iso_for_journal("2026-08-19T18:33:36Z")
    assert kernlog._shift_seconds(stamp, 1) == f"@{int(stamp[1:]) + 1}"


def test_shift_leaves_an_unrecognised_bound_alone():
    assert kernlog._shift_seconds("not a stamp", 1) == "not a stamp"


def test_a_line_in_two_abutting_epoch_windows_is_counted_once(monkeypatch):
    """Widening --until can show one line to two consecutive epochs."""
    history = ident.LocatorHistory(epochs=[
        ident.LocatorEpoch(hctl="1:0:0:0", usb_port_path="2-2",
                           kernel_name="sdb",
                           valid_from="2026-08-19T18:00:00Z",
                           valid_until="2026-08-19T18:33:36Z"),
        ident.LocatorEpoch(hctl="1:0:0:0", usb_port_path="2-2",
                           kernel_name="sdb",
                           valid_from="2026-08-19T18:33:36Z",
                           valid_until="2026-08-19T19:00:00Z"),
    ])
    line = ("2026-08-19T18:33:36+0000 host kernel: "
            "usb 2-2: USB disconnect, device number 11")
    monkeypatch.setattr(kernlog, "_journal", lambda since, until=None: [line])

    events = kernlog.sweep(history, "2026-08-19T18:00:00Z")
    assert events.counts["usb_resets"] == 1, \
        "the same disconnect must not be counted once per overlapping epoch"
    assert events.lines == [line]


def test_journal_coverage_separates_a_clean_run_from_lost_evidence(monkeypatch):
    """An empty sweep is ambiguous; coverage resolves it.

    "No faults found" and "the log has been rotated away" both produce zero
    matched lines and mean opposite things. Reporting a rotated-away run as
    clean would invent evidence -- the same failure mode as a report claiming
    coverage it never achieved.
    """
    monkeypatch.setattr(kernlog, "_journal", lambda since, until=None: [
        "2026-08-03T17:14:49+0000 host kernel: Linux version 7.0.0"])
    assert kernlog.journal_covers("2026-08-18T14:35:00Z") is True
    assert kernlog.journal_covers("2026-01-01T00:00:00Z") is False


def test_journal_coverage_is_false_when_the_journal_is_unavailable(monkeypatch):
    monkeypatch.setattr(kernlog, "_journal", lambda since, until=None: [])
    assert kernlog.journal_covers("2026-08-18T14:35:00Z") is False
