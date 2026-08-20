"""pytest configuration.

Tests that need losetup or device-mapper are marked @pytest.mark.root and skip
with a clear message when not run as root, rather than failing confusingly.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from driveprep import blockio, grade as grading  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "root: test requires root (losetup, device-mapper, mount)")


def pytest_collection_modifyitems(config, items):
    if os.geteuid() == 0:
        return
    skip = pytest.mark.skip(
        reason="requires root: this test builds real loop or device-mapper "
               "fixtures. Run as: sudo -E python3 -m pytest"
    )
    for item in items:
        if "root" in item.keywords:
            item.add_marker(skip)


def _require(tool: str):
    return pytest.mark.skipif(
        shutil.which(tool) is None, reason=f"{tool} not installed")


needs_losetup = _require("losetup")
needs_dmsetup = _require("dmsetup")
needs_smartctl = _require("smartctl")


@pytest.fixture
def config():
    return grading.load_config()


@pytest.fixture
def pass_config():
    return blockio.PassConfig(
        chunk_bytes=1024 * 1024,
        logical_block_bytes=512,
        physical_block_bytes=512,
    )


@pytest.fixture
def clean_report():
    """A report.json-shaped dict for a drive with nothing wrong with it."""
    return {
        "schema_version": 1,
        "report_id": "DP-20260802-test",
        "batch_id": "B-test",
        "tool": {"name": "driveprep", "version": "1.0.0"},
        "generated_utc": "2026-08-02T00:00:00Z",
        "drive": {
            "by_id": "usb-Test_Drive_0001-0:0",
            "model": "TEST HDD", "vendor": "Test",
            "capacity_bytes": 512 * 1024 * 1024, "capacity_label": "537 MB",
            "bus_type": "usb", "rotation_rpm": 5400,
            "logical_block_bytes": 512, "physical_block_bytes": 512,
        },
        "smart": {
            "available": True, "overall_health": "PASSED",
            "power_on_hours": 1200, "power_cycles": 40,
            "attributes": [
                {"id": 5, "name": "Reallocated_Sector_Ct", "value": 200,
                 "worst": 200, "thresh": 140, "raw": 0, "flags": "PO--CK",
                 "when_failed": ""},
            ],
            "before_after_delta": [],
        },
        "self_tests": {
            "short": {"run": True, "status": "completed_without_error",
                      "duration_s": 90, "lba_of_first_error": None},
            "extended": {"run": True, "status": "completed_without_error",
                         "duration_s": 900, "lba_of_first_error": None},
        },
        "erase": {
            "performed": True, "method": "single_pass_zero",
            "bytes_written": 512 * 1024 * 1024,
            "started_utc": "2026-08-02T00:00:00Z",
            "finished_utc": "2026-08-02T00:05:00Z", "duration_s": 300,
            "throughput_mean_mbs": 140.0, "throughput_min_mbs": 120.0,
            "throughput_max_mbs": 150.0,
        },
        "verify": {
            "performed": True, "bytes_read": 512 * 1024 * 1024,
            "nonzero_ranges": [], "read_error_ranges": [], "read_errors": 0,
            "duration_s": 280, "result": "all_zero_no_errors",
        },
        "run_conditions": {
            "temp_max_c": 38, "thermal_pause_s": 0, "disconnects": 0,
            "kernel_events": {"io_errors": 0, "medium_errors": 0,
                              "usb_resets": 0, "uas_aborts": 0},
        },
        "flags": {
            "skipped_extended_test": False,
            "smart_via_bridge_unavailable": False,
            "thermally_aborted": False,
        },
        # A real report.json always carries its grade; report.py renders the
        # badge straight from it. Tests that mutate the report recompute this.
        "grade": {"value": "PASS", "reasons": [], "rubric_version": 1},
    }
