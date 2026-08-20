"""Identity tuples per device class: spec 14 test 15.

The original spec computed one SCSI-shaped tuple for every device. Loop and dm
devices have no /sys/block/<kname>/device/ directory at all, so that tuple was
unimplementable for every section 14 fixture -- and every destructive test runs
through those.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from driveprep import identity as ident, inventory as inv
from make_loop import loop_device

from conftest import needs_dmsetup, needs_losetup


def test_15_loop_devices_have_no_scsi_device_directory():
    """Fails loudly if anyone reintroduces the SCSI-only assumption."""
    loops = sorted(Path("/sys/block").glob("loop*"))
    if not loops:
        pytest.skip("no loop devices on this host")
    for sysdir in loops[:3]:
        assert not (sysdir / "device").exists(), \
            f"{sysdir}/device unexpectedly exists; the per-class tuple " \
            f"assumption needs revisiting"
        assert (sysdir / "loop").is_dir()
        assert ident.classify(sysdir) == ident.CLASS_LOOP


def test_15_classify_dispatches_on_the_right_subdirectory():
    seen = set()
    for sysdir in sorted(Path("/sys/block").iterdir()):
        try:
            seen.add(ident.classify(sysdir))
        except ident.IdentityError:
            continue
    assert seen, "no classifiable block devices at all"
    assert seen <= {ident.CLASS_SCSI, ident.CLASS_LOOP, ident.CLASS_DM}


def test_15_loop_identity_uses_backing_file_and_offset():
    loops = [p.name for p in sorted(Path("/sys/block").glob("loop*"))
             if int((p / "size").read_text().strip()) > 0]
    if not loops:
        pytest.skip("no active loop devices on this host")
    identity = ident.identity_of(loops[0])
    assert identity.cls == ident.CLASS_LOOP
    assert identity.id_model == "loop"
    assert ":" in identity.id_serial, "serial is backing_file:offset"
    assert identity.size_bytes > 0


def test_15_scsi_identity_uses_model_and_serial():
    disks = [d for d in inv.scan() if d.device_class == ident.CLASS_SCSI]
    if not disks:
        pytest.skip("no SCSI/ATA disks on this host")
    identity = ident.identity_of(disks[0].kname)
    assert identity.cls == ident.CLASS_SCSI
    assert identity.id_model, "sysfs model must be non-empty for a real disk"


def test_15_class_participates_in_equality():
    """A loop device must never satisfy a real disk's recorded identity."""
    loop = ident.Identity(ident.CLASS_LOOP, 1 << 30, "loop", "/tmp/x.img:0")
    scsi = ident.Identity(ident.CLASS_SCSI, 1 << 30, "loop", "/tmp/x.img:0")
    assert loop != scsi
    assert "class" in loop.describe_mismatch(scsi)


def test_15_identity_round_trips_through_json():
    for identity in (
        ident.Identity(ident.CLASS_SCSI, 4000787030016, "Elements 25A2",
                       "575834314235"),
        ident.Identity(ident.CLASS_LOOP, 1 << 20, "loop", "/tmp/a.img:0"),
        ident.Identity(ident.CLASS_DM, 1 << 20, "dm", "CRYPT-LUKS2-abc"),
    ):
        assert ident.Identity.from_json(identity.to_json()) == identity


def test_15_size_bytes_always_multiplies_sysfs_size_by_512():
    """sysfs size is in 512-byte units even on 4Kn -- an 8x overrun bug."""
    for sysdir in sorted(Path("/sys/block").iterdir()):
        sectors = int((sysdir / "size").read_text().strip())
        if sectors == 0:
            continue
        assert ident.size_bytes_of(sysdir) == sectors * 512
        return


def test_15_locators_are_none_for_loop_devices():
    loops = [p.name for p in sorted(Path("/sys/block").glob("loop*"))]
    if not loops:
        pytest.skip("no loop devices")
    assert ident.locator_of(loops[0]) == (None, None)


def test_15_locator_history_records_epochs():
    history = ident.LocatorHistory()
    history.open_epoch("sdc", when="2026-08-02T03:00:00Z")
    history.open_epoch("sdd", when="2026-08-02T09:00:00Z")
    assert len(history.epochs) == 2
    assert history.epochs[0].valid_until == "2026-08-02T09:00:00Z"
    assert history.epochs[1].valid_until is None
    assert history.current.kernel_name == "sdd"
    restored = ident.LocatorHistory.from_json(history.to_json())
    assert restored.epochs[0].kernel_name == "sdc"


@pytest.mark.root
@needs_losetup
def test_15_identity_from_open_fd_matches_and_detects_mismatch():
    """Step 3 of the spec 4.5 sequence, on a real fixture."""
    with loop_device(16 * 1024 * 1024) as loop:
        recorded = ident.identity_of(loop.kname)
        fd = os.open(loop.path, os.O_RDONLY)
        try:
            found, kname = ident.identity_of_fd(fd)
            assert kname == loop.kname
            assert found == recorded
            assert ident.assert_matches(recorded, fd) == loop.kname

            wrong = ident.Identity(recorded.cls, recorded.size_bytes + 4096,
                                   recorded.id_model, recorded.id_serial)
            with pytest.raises(ident.IdentityError, match="identity mismatch"):
                ident.assert_matches(wrong, fd)
        finally:
            os.close(fd)


@pytest.mark.root
@needs_losetup
def test_15_guarded_open_succeeds_on_a_loop_fixture():
    """Everything destructive in the suite depends on this working."""
    from driveprep import safety
    with loop_device(16 * 1024 * 1024) as loop:
        disk = next(d for d in inv.scan() if d.kname == loop.kname)
        with safety.guarded_open(disk, disk.identity, write=True,
                                 test_mode=True) as (fd, kname):
            assert kname == loop.kname
            assert os.write is not None
            assert fd > 0


@pytest.mark.root
@needs_dmsetup
@needs_losetup
def test_15_dm_identity_uses_uuid_or_name():
    from make_flaky import dm_error_device
    with loop_device(16 * 1024 * 1024) as loop:
        with dm_error_device(loop.path, loop.size_bytes,
                             [(4 * 1024 * 1024, 1024 * 1024)]) as dm:
            identity = ident.identity_of(dm.kname)
            assert identity.cls == ident.CLASS_DM
            assert identity.id_model == "dm"
            assert identity.id_serial
