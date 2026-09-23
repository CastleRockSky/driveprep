"""--mask-serial must reach every place the serial is printed.

It used to mask only the two serial fields. The by-id name carries the serial
too -- an Elements enclosure's is the drive serial, hex-encoded -- and the
report id built from it is printed in the PNG footer, so the listing image
showed the very value the flag exists to hide.
"""
import json

import pytest

from driveprep import identity as ident
from driveprep import inventory as inv, pipeline as pipe, state as st

SERIAL = "TESTSERIAL01"
HEX = SERIAL.encode().hex()   # 5445535453455249414c3031


class Opts:
    def __init__(self, tmp_path, mask):
        self.output_root = str(tmp_path)
        self.seller_name = ""
        self.mask_serial = mask
        self.verbose = False
        self.skip_extended_test = False
        self.stop_on_fail = False
        self.chunk_size = None


def _report(tmp_path, by_id, serial, mask=True):
    disk = inv.Disk(
        kname="sdz", by_id=by_id, synthetic_id=None,
        identity=ident.Identity(ident.CLASS_SCSI, 1 << 30, "Elements 25A2",
                                serial),
        bus_type="usb", size_bytes=1 << 30, logical_block_bytes=512,
        physical_block_bytes=512, sysfs_rotational=1, read_only=False,
        device_class="scsi", model="Elements 25A2", serial=serial)
    state = st.DriveState(
        drive_id=by_id, output_dir=tmp_path, batch_id="B", run_id="R",
        smartctl_d_type="sat", smart_before_data=None, smart_available=False,
        capacity_bytes=1 << 30)
    state.identity = disk.identity
    state.ata_serial = "WD-TESTAAAA0001"
    p = pipe.DrivePipeline(disk, state, {"io": {}, "checkpoint": {}},
                           Opts(tmp_path, mask))
    return p.build_report()


@pytest.mark.parametrize("by_id,serial", [
    (f"usb-WD_Elements_25A2_{HEX}-0:0", HEX),        # hex in by-id and udev
    (f"usb-WD_Elements_25A2_{HEX}-0:0", SERIAL),     # hex in by-id only
    (f"usb-Dock_{SERIAL}-0:0", SERIAL),
    ("ata-WDC_WD40EZRZ-00GXCB0_WD-TESTAAAA0001", SERIAL),  # SATA: ATA serial
])
def test_no_serial_survives_anywhere_in_a_masked_report(tmp_path, by_id,
                                                        serial):
    text = json.dumps(_report(tmp_path, by_id, serial))
    for secret in (SERIAL, HEX, HEX.upper(), "WD-TESTAAAA0001"):
        assert secret not in text, f"{secret} leaked through --mask-serial"


def test_the_masked_report_still_identifies_the_unit(tmp_path):
    report = _report(tmp_path, f"usb-WD_Elements_25A2_{HEX}-0:0", HEX)
    assert report["report_id"].startswith("DP-")
    assert "usb-WD_Elements_25A2_544" in report["report_id"]
    assert report["drive"]["by_id"].endswith("031-0:0")


def test_an_unmasked_report_is_unchanged(tmp_path):
    by_id = f"usb-WD_Elements_25A2_{HEX}-0:0"
    report = _report(tmp_path, by_id, HEX, mask=False)
    assert report["drive"]["by_id"] == by_id
    assert HEX in report["report_id"]
