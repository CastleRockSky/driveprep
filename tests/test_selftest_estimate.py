"""A drive's self-test estimate must be floored by what physics allows.

Written after a 4 TB Hitachi HUS724040ALE641 reported a ONE minute extended
self-test. The stall deadline became three minutes, the run declared the test
inconclusive at the second poll -- ten minutes in -- and graded the drive
CAUTION for it. The drive carried on scanning for another nine hours and
passed. The HGST in the next bay reported 551 minutes and behaved correctly.
"""
import pytest

from driveprep import identity as ident, inventory as inv
from driveprep import pipeline as pipe, smart, state as st


FOUR_TB = 4_000_787_030_016
EIGHT_TB = 8_001_563_222_016


class Opts:
    def __init__(self, tmp_path):
        self.output_root = str(tmp_path)
        self.seller_name = ""
        self.mask_serial = False
        self.verbose = False
        self.skip_extended_test = False
        self.stop_on_fail = False
        self.chunk_size = None


def _disk(size_bytes=FOUR_TB):
    return inv.Disk(
        kname="sdz", by_id="usb-Test_0001-0:0", synthetic_id=None,
        identity=ident.Identity(ident.CLASS_SCSI, size_bytes, "TestModel",
                                "TESTSERIAL01"),
        bus_type="usb", size_bytes=size_bytes, logical_block_bytes=512,
        physical_block_bytes=512, sysfs_rotational=1, read_only=False,
        device_class="scsi", model="Test Model", serial="TESTSERIAL01")


def _smart_data(extended_minutes):
    """Shaped like smartctl --json, carrying a polling estimate."""
    return {
        "smart_status": {"passed": True},
        "power_on_time": {"hours": 50000},
        "ata_smart_attributes": {"table": []},
        "ata_smart_data": {
            "self_test": {
                "polling_minutes": {"short": 1, "extended": extended_minutes},
            },
        },
    }


def _pipeline(tmp_path, extended_minutes, size_bytes=FOUR_TB):
    state = st.DriveState(
        drive_id="usb-Test_0001-0:0", output_dir=tmp_path, batch_id="B",
        run_id="R", smartctl_d_type="sat",
        smart_before_data=_smart_data(extended_minutes),
        smart_available=True, capacity_bytes=size_bytes)
    return pipe.DrivePipeline(_disk(size_bytes), state,
                              {"io": {}, "checkpoint": {}, "selftest": {}},
                              Opts(tmp_path))


# --------------------------------------------------------------------------
# The floor itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size_bytes,at_least", [
    (FOUR_TB, 250),      # ~267 min
    (EIGHT_TB, 500),     # ~533 min
    (500_107_862_016, 30),
])
def test_the_floor_scales_with_capacity(size_bytes, at_least):
    assert smart.extended_test_floor_minutes(size_bytes) >= at_least


def test_an_unknown_capacity_yields_no_floor():
    """Nothing is known, so nothing is claimed -- the caller's fallback wins."""
    assert smart.extended_test_floor_minutes(None) == 0
    assert smart.extended_test_floor_minutes(0) == 0


def test_the_floor_is_conservative_enough_to_pass_real_drives_through():
    """A correct drive estimate must survive untouched.

    The HGST reports 551 minutes for 4 TB and the 8 TB WD reports 1074. If the
    floor exceeded either, this fix would start distorting healthy runs.
    """
    assert smart.extended_test_floor_minutes(FOUR_TB) < 551
    assert smart.extended_test_floor_minutes(EIGHT_TB) < 1074


# --------------------------------------------------------------------------
# The pipeline applying it
# --------------------------------------------------------------------------


def test_a_one_minute_estimate_for_4tb_is_replaced(tmp_path):
    """The regression, with the exact value the Hitachi reported."""
    p = _pipeline(tmp_path, extended_minutes=1)
    estimate = p._polling_estimate("extended")

    assert estimate >= 250, "a 4 TB surface scan cannot take one minute"
    # The bug in the units that mattered: stall deadline vs the 300 s poll.
    stall_deadline_s = estimate * 60 * 3.0
    assert stall_deadline_s > 9 * 3600, \
        "the deadline must outlast a real nine-hour test"


def test_a_sane_estimate_is_left_alone(tmp_path):
    """551 minutes is the HGST's real answer and must pass through."""
    p = _pipeline(tmp_path, extended_minutes=551)
    assert p._polling_estimate("extended") == 551


def test_a_missing_estimate_falls_back_to_the_floor(tmp_path):
    """Better than run_selftest's blind five-minute default."""
    p = _pipeline(tmp_path, extended_minutes=None)
    assert p._polling_estimate("extended") >= 250


def test_the_short_test_is_not_floored(tmp_path):
    """A short test scans no surface; one minute is a truthful answer."""
    p = _pipeline(tmp_path, extended_minutes=1)
    assert p._polling_estimate("short") == 1


def test_the_manifest_estimate_is_floored_too(tmp_path):
    """Phase 6 is part of the quoted wall clock, so it was wrong there as well."""
    from driveprep import __main__ as main
    state = st.DriveState(
        drive_id="d", output_dir=tmp_path, batch_id="B", run_id="R",
        smartctl_d_type="sat", smart_before_data=_smart_data(1),
        smart_available=True, capacity_bytes=FOUR_TB)
    seconds = main._extended_test_seconds(state, one_pass=1000.0)
    assert seconds >= 250 * 60, "an hour-long quote for a nine-hour phase"
