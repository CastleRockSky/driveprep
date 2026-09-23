"""Which attached device may a continuing run write to?

A reconnect mid-pass and `resume` both write to a drive without a fresh
confirmation, so the match has to be unambiguous. Both used to take the first
device whose identity tuple matched -- and on a bridge exposing no serial that
tuple is only class + size + model.
"""
import pytest

from driveprep import identity as ident, inventory as inv
from driveprep import pipeline as pipe, smart, state as st

SIZE = 4_000_787_030_016


def _disk(kname, serial="TESTSERIAL01", model="WDC WD40EZRZ-00GXCB0"):
    return inv.Disk(
        kname=kname, by_id=f"usb-Test_{kname}-0:0", synthetic_id=None,
        identity=ident.Identity(ident.CLASS_SCSI, SIZE, model, serial),
        bus_type="usb", size_bytes=SIZE, logical_block_bytes=512,
        physical_block_bytes=4096, sysfs_rotational=1, read_only=False,
        device_class="scsi", model=model, serial=serial)


def _state(tmp_path, serial="TESTSERIAL01", ata_serial="WD-TESTAAAA0001"):
    state = st.DriveState(
        drive_id="usb-Test_sdc-0:0", output_dir=tmp_path, batch_id="B",
        run_id="R", smartctl_d_type="sat", smart_before_data=None,
        smart_available=True, capacity_bytes=SIZE)
    state.identity = _disk("sdc", serial).identity
    state.ata_serial = ata_serial
    return state


@pytest.fixture
def ata_serials(monkeypatch):
    """kname -> the ATA serial smartctl reports for it."""
    table = {}

    def refresh(dev, d_type):
        kname = dev.rsplit("/", 1)[-1].replace("usb-Test_", "").split("-")[0]
        return smart.SmartResult(True, d_type,
                                 {"serial_number": table.get(kname)})

    monkeypatch.setattr(smart, "refresh", refresh)
    return table


def test_a_serial_bearing_identity_matches_on_its_own(tmp_path, ata_serials):
    disk, refusal = pipe.match_stored_drive([_disk("sdf")], _state(tmp_path))
    assert disk.kname == "sdf" and refusal is None


def test_nothing_attached_is_absent_not_refused(tmp_path, ata_serials):
    """The caller keeps waiting: the drive may still come back."""
    assert pipe.match_stored_drive([], _state(tmp_path)) == (None, None)


def test_two_matching_devices_are_refused(tmp_path, ata_serials):
    disks = [_disk("sdf", ""), _disk("sdg", "")]
    disk, refusal = pipe.match_stored_drive(disks, _state(tmp_path, serial=""))
    assert disk is None
    assert "sdf, sdg" in refusal


def test_no_serial_is_confirmed_by_the_ata_serial(tmp_path, ata_serials):
    ata_serials["sdf"] = "WD-TESTAAAA0001"
    disk, refusal = pipe.match_stored_drive([_disk("sdf", "")],
                                            _state(tmp_path, serial=""))
    assert disk.kname == "sdf" and refusal is None


def test_a_lookalike_drive_is_refused(tmp_path, ata_serials):
    """The regression: same model, same size, different drive, still full of data."""
    ata_serials["sdf"] = "WD-TESTBBBB0002"
    disk, refusal = pipe.match_stored_drive([_disk("sdf", "")],
                                            _state(tmp_path, serial=""))
    assert disk is None
    assert "WD-TESTBBBB0002" in refusal


def test_an_unreadable_ata_serial_is_refused(tmp_path, ata_serials):
    disk, refusal = pipe.match_stored_drive([_disk("sdf", "")],
                                            _state(tmp_path, serial=""))
    assert disk is None and "unreadable" in refusal


def test_no_serial_anywhere_is_refused(tmp_path, ata_serials):
    ata_serials["sdf"] = "WD-TESTAAAA0001"
    disk, refusal = pipe.match_stored_drive(
        [_disk("sdf", "")], _state(tmp_path, serial="", ata_serial=""))
    assert disk is None and "cannot be confirmed" in refusal


def test_a_refusal_on_reconnect_aborts_instead_of_writing(tmp_path,
                                                          ata_serials,
                                                          monkeypatch):
    ata_serials["sdf"] = "WD-TESTBBBB0002"
    monkeypatch.setattr(inv, "scan", lambda: [_disk("sdf", "")])
    monkeypatch.setattr(pipe.time, "sleep", lambda s: None)

    class Opts:
        chunk_size = None

    p = pipe.DrivePipeline(_disk("sdc", ""), _state(tmp_path, serial=""),
                           {"io": {}, "checkpoint": {}}, Opts())
    with pytest.raises(pipe.DriveAborted, match="on reconnect"):
        p._await_return(60)
