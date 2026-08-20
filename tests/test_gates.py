"""Pre-erase SMART gate and --stop-on-fail.

Both exist to stop spending hours on a drive whose outcome is already decided.
Both must fail loudly rather than quietly narrow what gets recorded.
"""

from __future__ import annotations

import copy

import pytest

from driveprep import grade as grading, pipeline as pipe, state as st


class Opts:
    def __init__(self, tmp_path, stop_on_fail=False):
        self.chunk_size = None
        self.test_mode = True
        self.output_root = str(tmp_path)
        self.seller_name = ""
        self.mask_serial = False
        self.verbose = False
        self.skip_extended_test = False
        self.stop_on_fail = stop_on_fail


def _pipeline(tmp_path, config, smart_data, stop_on_fail=False):
    state = st.DriveState(drive_id="d", output_dir=tmp_path, batch_id="B",
                          run_id="R", smartctl_d_type="sat",
                          smart_before_data=smart_data,
                          smart_available=smart_data is not None,
                          capacity_bytes=1 << 30)
    disk = type("D", (), {"logical_block_bytes": 512,
                          "physical_block_bytes": 512, "id": "d",
                          "size_bytes": 1 << 30})()
    return pipe.DrivePipeline(disk, state, config, Opts(tmp_path, stop_on_fail))


def _smart(attrs=None):
    """attrs maps SMART attribute id -> raw value (ids are ints, not kwargs)."""
    table = [{"id": i, "name": f"A{i}", "value": 100, "worst": 100,
              "thresh": 0, "when_failed": "", "raw": {"value": v}}
             for i, v in (attrs or {}).items()]
    return {"smart_status": {"passed": True},
            "power_on_time": {"hours": 100},
            "ata_smart_attributes": {"table": table}}


# --------------------------------------------------------------------------
# Pre-erase SMART gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize("attrs,fragment", [
    ({197: 45}, "Current Pending Sector"),
    ({198: 3}, "Offline Uncorrectable"),
    ({187: 12}, "Reported Uncorrectable"),
])
def test_a_drive_already_failing_on_smart_is_not_erased(tmp_path, config,
                                                        attrs, fragment):
    """Eight hours of writing cannot change an outcome already decided."""
    p = _pipeline(tmp_path, config, _smart(attrs))
    reasons = p.smart_gate()
    assert reasons, "the gate must fire"
    assert any(fragment in r for r in reasons)
    assert all(r.startswith("FAIL") for r in reasons)
    assert p.state.failed_reason


def test_failed_smart_health_is_gated(tmp_path, config):
    data = _smart()
    data["smart_status"] = {"passed": False}
    assert _pipeline(tmp_path, config, data).smart_gate()


def test_a_healthy_drive_passes_the_gate(tmp_path, config):
    p = _pipeline(tmp_path, config, _smart({5: 0, 197: 0, 198: 0}))
    assert p.smart_gate() == []


def test_caution_conditions_do_not_gate(tmp_path, config):
    """Only FAIL conditions skip the erase; a CAUTION drive is still worth doing."""
    p = _pipeline(tmp_path, config, _smart({5: 8, 199: 14}))
    assert p.smart_gate() == [], \
        "reallocated sectors are CAUTION, not a reason to refuse the work"


def test_the_gate_is_silent_when_smart_is_unavailable(tmp_path, config):
    """No SMART is not evidence against a drive (spec 6.2)."""
    assert _pipeline(tmp_path, config, None).smart_gate() == []


def test_the_gate_cannot_fire_on_surface_findings(tmp_path, config):
    """It runs before any reading, so it must only use pre-known conditions."""
    p = _pipeline(tmp_path, config, _smart({5: 0}))
    p.state.verify_findings.read_errors = 99
    assert p.smart_gate() == [], \
        "the gate must not consult findings that do not exist yet"


# --------------------------------------------------------------------------
# --stop-on-fail
# --------------------------------------------------------------------------


def test_stop_on_fail_is_off_by_default(tmp_path, config):
    from driveprep.__main__ import _normalize, build_parser
    args = _normalize(build_parser().parse_args(["run", "--all", "--execute"]))
    assert args.stop_on_fail is False, \
        "a truncated run records less evidence; it must be opt-in"


def test_stop_requested_only_fires_when_enabled(tmp_path, config):
    off = _pipeline(tmp_path, config, _smart(), stop_on_fail=False)
    off.state.verify_findings.read_errors = 5
    assert off._stop_requested() is False

    on = _pipeline(tmp_path, config, _smart(), stop_on_fail=True)
    on.state.verify_findings.read_errors = 5
    assert on._stop_requested() is True
    assert on.state.stopped_on_fail is True


def test_stop_on_fail_does_not_fire_on_a_clean_pass(tmp_path, config):
    p = _pipeline(tmp_path, config, _smart(), stop_on_fail=True)
    assert p._stop_requested() is False
    assert p.state.stopped_on_fail is False


def test_stop_on_fail_skips_the_extended_test(tmp_path, config):
    p = _pipeline(tmp_path, config, _smart(), stop_on_fail=True)
    p.state.verify_findings.read_errors = 1
    p.phase6_extended_test()
    assert p.state.extended_test["status"] == "skipped_already_failed"
    assert p.state.extended_test["run"] is False


def test_a_truncated_verify_makes_no_positive_claim(clean_report, tmp_path):
    """It found errors; it must not also claim it checked the whole device."""
    from driveprep import report as reporting
    report = copy.deepcopy(clean_report)
    report["verify"] = {"performed": False, "bytes_read": 500_000_000,
                        "read_errors": 38, "nonzero_ranges": [],
                        "read_error_ranges": [{"first_lba": 4096}],
                        "stopped_on_fail": True}
    path = tmp_path / "r.html"
    reporting.render_html(report, path)
    html = path.read_text()
    assert "stopped at the first failure" in html
    assert "rest of the drive was not checked" in html
    assert "likely greater than shown" in html
    assert "read back as zero, with 0 read errors" not in html
