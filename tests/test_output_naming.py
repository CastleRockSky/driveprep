"""Two same-model drives in one dock bay must not share a directory."""
import json

import pytest

from driveprep import inventory as inv


def _disk(by_id, serial, kname="sdd"):
    return inv.Disk(
        kname=kname, by_id=by_id, synthetic_id=None,
        identity=inv.ident.Identity(inv.ident.CLASS_SCSI, 2000398934016,
                                    "TestModel2TB", serial),
        bus_type="usb", size_bytes=2000398934016, logical_block_bytes=512,
        physical_block_bytes=512, sysfs_rotational=1, read_only=False,
        device_class="scsi", model="Test Model 2TB", serial=serial,
    )


DOCK = "usb-Test_Model_2TB-00A00A00_ENCL0000000-0:1"


def test_two_drives_in_one_bay_get_separate_directories():
    """The exact collision: a dock reports ITS serial, not the drive's.

    Both drives are the same model and pass through the same bay, so their
    by-id names are byte-identical. Sharing a directory means the second run
    overwrites the first drive's completed report.
    """
    first = _disk(DOCK, "WD-TESTAAAA0001")
    second = _disk(DOCK, "WD-TESTBBBB0002")
    assert first.id == second.id, "precondition: the identifier cannot tell them apart"
    assert first.output_name != second.output_name
    assert "WD-TESTAAAA0001" in first.output_name
    assert "WD-TESTBBBB0002" in second.output_name


def test_a_pass_through_enclosure_keeps_its_directory_name():
    """Serial already in the name, plain: do not rename what was never ambiguous."""
    disk = _disk("ata-Test_Model_1TB-1ER162_TS00000001", "TS00000001")
    assert disk.output_name == "ata-Test_Model_1TB-1ER162_TS00000001"


def test_a_hex_encoded_serial_counts_as_carried():
    """udev hex-encodes some serials; 544553... IS TESTSERIAL01.

    Missing this would append a serial the name already carries and rename
    every single-enclosure directory on disk.
    """
    disk = _disk("usb-Test_Enclosure_25A1_5445535453455249414c3031-0:0",
                 "TESTSERIAL01")
    assert disk.output_name == "usb-Test_Enclosure_25A1_5445535453455249414c3031-0_0"


def test_an_unreadable_serial_falls_back_to_the_bay_name():
    disk = _disk("usb-Nameless-0:0", "")
    assert disk.output_name == "usb-Nameless-0_0"


# --------------------------------------------------------------------------
# Adoption and refusal
# --------------------------------------------------------------------------

def _stored(tmp_path, name, serial, kind="report"):
    d = tmp_path / name
    d.mkdir()
    if kind == "report":
        (d / "report.json").write_text(json.dumps(
            {"drive": {"ata_serial": serial}}))
    else:
        (d / "state.json").write_text(json.dumps({"enclosure_serial": serial}))
    return d


def test_a_drives_own_older_directory_is_adopted(tmp_path):
    """Renaming the scheme must not strand a drive's own history.

    The pre-serial directory holds THIS drive's past run, so the new run
    continues it instead of starting a second directory beside it.
    """
    from driveprep.__main__ import _output_dir_for

    legacy = _stored(tmp_path, inv.sanitize_id(DOCK), "WD-TESTAAAA0001")
    disk = _disk(DOCK, "WD-TESTAAAA0001")
    directory, problem = _output_dir_for(tmp_path, disk)
    assert problem is None
    assert directory == legacy


def test_a_directory_belonging_to_another_drive_is_refused(tmp_path):
    """The destructive case, stated directly.

    A directory recording drive A must never be handed to drive B. Before
    this, the run mkdir'd it with exist_ok and force-checkpointed over the
    top, destroying a completed report for a drive that may already be packed
    and listed.
    """
    from driveprep.__main__ import _output_dir_for

    # Same directory name for both, which is what an unreadable serial does.
    _stored(tmp_path, "usb-Nameless-0_0", "WD-AAAA")
    intruder = _disk("usb-Nameless-0:0", "")
    directory, problem = _output_dir_for(tmp_path, intruder)

    assert directory.name == "usb-Nameless-0_0"
    assert problem is not None, "must not silently reuse another drive's dir"
    assert "WD-AAAA" in problem
    assert "will not be overwritten" in problem


def test_an_unreadable_serial_cannot_claim_a_directory(tmp_path):
    """Refusing an unknown serial is the deliberate direction of the trade.

    It cannot prove the directory is its own. The choice is between refusing a
    re-run and overwriting a finished report on a guess, so it refuses and
    says how to proceed.
    """
    from driveprep.__main__ import _output_dir_for

    _stored(tmp_path, "usb-Nameless-0_0", "WD-AAAA", kind="state")
    _, problem = _output_dir_for(tmp_path, _disk("usb-Nameless-0:0", ""))
    assert problem is not None
    assert "cannot be read" in problem


def test_a_drive_re_running_into_its_own_directory_is_allowed(tmp_path):
    """The guard must not block the ordinary case it sits next to."""
    from driveprep.__main__ import _output_dir_for

    _stored(tmp_path, "usb-Box_WD-AAAA-0_0", "WD-AAAA")
    disk = _disk("usb-Box_WD-AAAA-0:0", "WD-AAAA")
    assert disk.output_name == "usb-Box_WD-AAAA-0_0", "serial already in the name"
    directory, problem = _output_dir_for(tmp_path, disk)
    assert problem is None
    assert directory.name == "usb-Box_WD-AAAA-0_0"


def test_the_run_refuses_rather_than_starting(tmp_path, monkeypatch, capsys):
    """End to end: the refusal reaches the operator and stops the batch."""
    from driveprep import __main__ as cli

    _stored(tmp_path, "usb-Nameless-0_0", "WD-AAAA")

    class Opts:
        output_root = str(tmp_path)
        batch_id = "B-TEST"
        verbose = False

    with pytest.raises(cli._OutputCollision) as caught:
        cli._prepare_states([_disk("usb-Nameless-0:0", "")], Opts(),
                            tmp_path)
    assert "WD-AAAA" in str(caught.value)


def test_state_json_identifies_the_owner_when_no_report_exists(tmp_path):
    """An interrupted run has state.json and no report.json yet."""
    from driveprep.__main__ import _stored_serial

    d = _stored(tmp_path, "d", "WD-ABC", kind="state")
    assert _stored_serial(d) == "WD-ABC"
    assert _stored_serial(tmp_path / "nothing-here") is None
