"""Safety layer: spec 14 tests 7, 8, 9, 14."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from driveprep import identity as ident, inventory as inv, safety
from make_loop import loop_device, make_partition

from conftest import needs_dmsetup, needs_losetup


def _disk(kname: str) -> inv.Disk:
    for disk in inv.scan():
        if disk.kname == kname:
            return disk
    raise AssertionError(f"{kname} not found in inventory")


def _fake_disk(**over) -> inv.Disk:
    base = dict(
        kname="sdz", by_id="usb-Test_0001-0:0", synthetic_id=None,
        identity=ident.Identity("scsi", 1 << 30, "Test", "0001"),
        bus_type="usb", size_bytes=1 << 30, logical_block_bytes=512,
        physical_block_bytes=512, sysfs_rotational=1, read_only=False,
        device_class="scsi", model="Test", serial="0001",
    )
    base.update(over)
    return inv.Disk(**base)


# --------------------------------------------------------------------------
# Test 9: confirmation token
# --------------------------------------------------------------------------


def test_9_token_shape_and_stability():
    disks = [_fake_disk(by_id=f"usb-D{i}-0:0") for i in range(4)]
    token = safety.compute_token(disks)
    assert token.startswith("DP-4-")
    assert len(token.split("-")[2]) == 4
    assert safety.compute_token(list(reversed(disks))) == token, \
        "token must not depend on argument order"


def test_9_token_rejects_a_fifth_device():
    four = [_fake_disk(by_id=f"usb-D{i}-0:0") for i in range(4)]
    token = safety.compute_token(four)
    five = [*four, _fake_disk(by_id="usb-D4-0:0")]
    assert safety.compute_token(five) != token
    assert not safety.verify_token(token, safety.compute_token(five))


def test_9_token_changes_when_the_resumed_set_is_smaller():
    """resume recomputes its own token and rejects the original batch's."""
    four = [_fake_disk(by_id=f"usb-D{i}-0:0") for i in range(4)]
    original = safety.compute_token(four)
    resumed = safety.compute_token(four[:2])
    assert resumed != original
    assert resumed.startswith("DP-2-")
    assert not safety.verify_token(original, resumed)


def test_9_token_comparison_is_case_and_whitespace_insensitive():
    token = safety.compute_token([_fake_disk()])
    assert safety.verify_token(f"  {token.lower()}  ", token)
    assert not safety.verify_token("DP-1-0000", token)


def test_9_token_hash_input_is_pinned():
    """The hash input is defined exactly so tokens survive a version bump."""
    import hashlib
    disks = [_fake_disk(by_id="usb-B-0:0"), _fake_disk(by_id="usb-A-0:0")]
    expected_payload = "usb-A-0:0\nusb-B-0:0".encode("utf-8")
    digest = hashlib.sha1(expected_payload).hexdigest().upper()[:4]
    assert safety.compute_token(disks) == f"DP-2-{digest}"


def test_9_token_uses_synthetic_id_when_there_is_no_by_id():
    disk = _fake_disk(by_id=None, synthetic_id="dp-abc123def456")
    assert safety.identity_string(disk) == "dp-abc123def456"
    assert safety.compute_token([disk]).startswith("DP-1-")


# --------------------------------------------------------------------------
# Test 7: safety refusals (pure-logic half)
# --------------------------------------------------------------------------


def test_7_nvme_is_refused_by_name():
    reasons = safety.check_structural(_fake_disk(kname="nvme0n1"), test_mode=False)
    assert any("NVMe" in r for r in reasons)


def test_7_loop_refused_outside_test_mode_permitted_inside():
    disk = _fake_disk(kname="loop9", device_class=ident.CLASS_LOOP)
    assert any("loop device" in r for r in safety.check_structural(disk, False))
    assert safety.check_structural(disk, test_mode=True) == []


def test_7_read_only_device_is_refused():
    assert safety.check_read_only(_fake_disk(read_only=True))


def test_7_solid_state_refused_only_on_positive_evidence():
    """Refusal requires positive evidence of solid state (spec 4.2).

    rotation_rate absent means unknown, which must be ELIGIBLE -- writing the
    rule the other way would make every SMART-blocked bridge and every test
    loop device unreachable.
    """
    disk = _fake_disk()
    assert safety.check_media_type(disk, {"rotation_rate": 0}), "SSD must refuse"
    assert safety.check_media_type(
        disk, {"_text": "Rotation Rate: Solid State Device"})
    assert safety.check_media_type(disk, {"rotation_rate": 5400}) == []
    assert safety.check_media_type(disk, {}) == [], \
        "absent rotation_rate must be ALLOWED, not refused"
    assert safety.check_media_type(disk, None) == []


def test_7_sysfs_rotational_alone_never_decides():
    """queue/rotational lies over USB; it must not gate eligibility."""
    disk = _fake_disk(sysfs_rotational=0)
    assert safety.check_media_type(disk, {"rotation_rate": 7200}) == []
    assert safety.check_media_type(disk, {}) == []


def test_7_usb_only_gate():
    assert safety.evaluate(_fake_disk(bus_type="ata"), usb_only=True,
                           protected=set())
    reasons = safety.evaluate(_fake_disk(bus_type="ata"), usb_only=False,
                              protected=set())
    assert not any("--usb-only" in r for r in reasons)


def test_7_drive_with_no_by_id_is_refused_outside_test_mode():
    disk = _fake_disk(by_id=None, synthetic_id="dp-deadbeef1234")
    reasons = safety.evaluate(disk, protected=set())
    assert any("by-id" in r for r in reasons)


def test_7_system_disk_is_refused():
    assert safety.check_system_disk(_fake_disk(kname="sda"), {"sda"})
    assert safety.check_system_disk(_fake_disk(kname="sdz"), {"sda"}) == []


def test_parent_disk_maps_partitions_back():
    """mdstat lists partition names; they must map to the parent disk."""
    for disk in inv.scan():
        if disk.partitions:
            part = disk.partitions[0]
            assert safety.parent_disk(part) == disk.kname
            return
    pytest.skip("no partitioned disk on this host")


def test_leaf_disks_recursion_reaches_real_disks():
    """The slaves/ recursion must handle multi-parent devices.

    lsblk -no PKNAME is single-valued and would resolve a mirrored root to one
    leg, leaving the other eligible.
    """
    protected = safety.protected_disks()
    assert protected, "the disk backing / must always resolve"
    for kname in protected:
        assert (Path("/sys/block") / kname).is_dir()
        assert not safety.is_partition(kname)


def test_the_running_system_disk_is_always_refused():
    """End-to-end: whatever backs / must never be eligible."""
    protected = safety.protected_disks()
    for disk in inv.scan():
        if disk.kname in protected:
            reasons = safety.evaluate(disk, protected=protected)
            assert reasons, f"{disk.kname} backs the system and must be refused"
            return
    pytest.skip("could not identify the system disk in inventory")


# --------------------------------------------------------------------------
# Test 14: --test-mode scope
# --------------------------------------------------------------------------


def test_14_test_mode_refuses_a_real_disk():
    disk = _fake_disk(device_class=ident.CLASS_SCSI)
    reasons = safety.evaluate(disk, test_mode=True, protected=set())
    assert any("loop and dm devices only" in r for r in reasons)


def test_14_test_mode_does_not_relax_mounted_or_readonly():
    """Every mounted snap loop device must still be refused."""
    checked = 0
    for disk in inv.scan():
        if disk.device_class != ident.CLASS_LOOP:
            continue
        reasons = safety.evaluate(disk, test_mode=True, protected=set())
        if disk.read_only or safety.check_mounted(disk):
            assert reasons, f"{disk.kname} is mounted or read-only and must refuse"
            checked += 1
    if not checked:
        pytest.skip("no mounted loop devices on this host")


def test_14_test_mode_with_all_is_refused_by_the_cli():
    from driveprep.__main__ import _select, _normalize, build_parser
    args = _normalize(build_parser().parse_args(
        ["run", "--test-mode", "--all", "--execute"]))
    with pytest.raises(SystemExit, match="may not be combined with --all"):
        _select(inv.scan(), args)


# --------------------------------------------------------------------------
# Test 7 (root half) and test 8: O_EXCL
# --------------------------------------------------------------------------


def _assert_partition_holder_detected(loop, part):
    """Shared assertions for the holders-on-a-partition case.

    This is the check most likely to be built wrong, and the one whose failure
    would let an in-use data disk get zeroed: /sys/block/sdb/holders/ is EMPTY
    when the LVM PV or dm target lives on sdb1 rather than sdb, because the
    holder symlink is at /sys/class/block/sdb1/holders/.
    """
    disk = _disk(loop.kname)
    assert part in disk.partitions, \
        f"{part} must be enumerated as a partition of {loop.kname}"

    # The whole disk's own holders directory is empty...
    whole = Path(f"/sys/block/{loop.kname}/holders")
    assert not list(whole.iterdir()), \
        "precondition: a naive whole-disk check would see nothing here"

    # ...but the holder on the partition must still be found.
    held = Path(f"/sys/class/block/{part}/holders")
    assert list(held.iterdir()), "fixture did not actually create a holder"

    reasons = safety.check_holders(disk)
    assert reasons, "holders on a partition must be detected"
    assert part in reasons[0]

    # And the disk must be ineligible overall, not merely flagged.
    assert safety.evaluate(disk, test_mode=True, protected=set())


@pytest.mark.root
@needs_losetup
@needs_dmsetup
def test_7_holders_on_a_partition_via_device_mapper():
    """Holders-on-a-partition, without depending on LVM.

    Preferred over the pvcreate fixture below: Ubuntu 24.04's LVM devices file
    (/etc/lvm/devices/system.devices) can refuse loop devices outright, which
    makes that fixture environment-dependent. A dm linear target stacked on the
    partition produces the identical sysfs holder relationship with only
    dmsetup, which the suite already requires.
    """
    if shutil.which("sfdisk") is None:
        pytest.skip("sfdisk not installed")

    with loop_device(128 * 1024 * 1024, partitioned=True) as loop:
        part = make_partition(loop)
        part_path = f"/dev/{part}"
        if not Path(part_path).exists():
            pytest.skip("kernel did not create the partition node")

        sectors = int(Path(f"/sys/class/block/{part}/size").read_text().strip())
        name = "driveprep-test-holder"
        subprocess.run(["dmsetup", "create", name],
                       input=f"0 {sectors} linear {part_path} 0\n",
                       text=True, check=True, capture_output=True)
        try:
            _assert_partition_holder_detected(loop, part)
        finally:
            subprocess.run(["dmsetup", "remove", "--force", name],
                           check=False, capture_output=True)


@pytest.mark.root
@needs_losetup
def test_7_inactive_lvm_pv_on_a_partition_is_refused():
    """An LVM PV is refused even with no holder present.

    `pvcreate` alone creates NO holder -- a holder only appears once a logical
    volume is activated on top. So the spec's suggested fixture does not
    exercise the holders path at all; what it actually exposed is that a
    deactivated volume group is invisible to every "currently active" check in
    section 4.2 while still owning the disk. The signature check is what
    catches it.
    """
    for tool in ("pvcreate", "sfdisk"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not installed")

    with loop_device(128 * 1024 * 1024, partitioned=True) as loop:
        part = make_partition(loop)
        part_path = f"/dev/{part}"
        if not Path(part_path).exists():
            pytest.skip("kernel did not create the partition node")

        created = subprocess.run(["pvcreate", "-ff", "-y", part_path],
                                 capture_output=True, text=True)
        if created.returncode != 0:
            pytest.skip(
                "pvcreate refused this loop device (usually the LVM devices "
                f"file on 24.04): {created.stderr.strip()[:200]}"
            )
        try:
            disk = _disk(loop.kname)

            # Precondition: this is invisible to the holders check.
            assert not list(Path(f"/sys/class/block/{part}/holders").iterdir())
            assert safety.check_holders(disk) == []

            # The signature check must still refuse it.
            reasons = safety.check_storage_signatures(disk)
            assert reasons, "an LVM PV must be refused even when inactive"
            assert "LVM2_member" in reasons[0]
            assert part in reasons[0]

            # And the drive must be ineligible overall.
            assert safety.evaluate(disk, test_mode=True, protected=set())
        finally:
            subprocess.run(["pvremove", "-ff", "-y", part_path],
                           check=False, capture_output=True)


@pytest.mark.root
@needs_losetup
def test_7_deactivated_volume_group_is_still_refused():
    """The scenario that would have destroyed a volume group.

    A disk carrying an LV, with the VG deactivated: holders empty, not mounted,
    not in mdstat, and its /dev/vg/lv fstab entry does not resolve. Every
    "currently active" rule passes it.
    """
    for tool in ("pvcreate", "vgcreate", "lvcreate", "vgchange", "sfdisk"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not installed")

    vg = "dpselftestvg"
    with loop_device(128 * 1024 * 1024, partitioned=True) as loop:
        part = make_partition(loop)
        part_path = f"/dev/{part}"
        if not Path(part_path).exists():
            pytest.skip("kernel did not create the partition node")

        if subprocess.run(["pvcreate", "-ff", "-y", part_path],
                          capture_output=True).returncode != 0:
            pytest.skip("pvcreate refused this loop device")
        try:
            subprocess.run(["vgcreate", vg, part_path],
                           check=True, capture_output=True)
            subprocess.run(["lvcreate", "-y", "-L", "32M", "-n", "lv", vg],
                           check=True, capture_output=True)
            disk = _disk(loop.kname)

            # While active, the holders check sees it.
            assert safety.check_holders(disk), "active LV must produce a holder"

            subprocess.run(["vgchange", "-an", vg], check=True,
                           capture_output=True)

            # Deactivated: the holders check now sees nothing at all.
            disk = _disk(loop.kname)
            assert safety.check_holders(disk) == [], \
                "precondition: deactivation removes the holder"

            # It must STILL be refused.
            assert safety.check_storage_signatures(disk), \
                "a deactivated volume group must not become eligible"
            assert safety.evaluate(disk, test_mode=True, protected=set())
        finally:
            subprocess.run(["vgchange", "-an", vg], check=False,
                           capture_output=True)
            subprocess.run(["vgremove", "-f", vg], check=False,
                           capture_output=True)
            subprocess.run(["pvremove", "-ff", "-y", part_path], check=False,
                           capture_output=True)


@pytest.mark.root
@needs_losetup
def test_7_fstab_referenced_but_unmounted_is_refused(tmp_path, monkeypatch):
    """An idle internal data disk in fstab passes every 'currently active' check."""
    with loop_device(32 * 1024 * 1024) as loop:
        fake_fstab = tmp_path / "fstab"
        fake_fstab.write_text(f"{loop.path}  /data  ext4  defaults  0  2\n")
        monkeypatch.setattr(safety, "ETC_FSTAB", fake_fstab)
        monkeypatch.setattr(safety, "ETC_CRYPTTAB", tmp_path / "absent")

        disk = _disk(loop.kname)
        assert not safety.check_mounted(disk), "fixture must not be mounted"
        reasons = safety.check_fstab_crypttab(disk)
        assert reasons and "fstab" in reasons[0]


@pytest.mark.root
@needs_losetup
def test_8_o_excl_conflicts_only_with_another_exclusive_opener():
    """Pins what O_EXCL actually does on a block device (spec 4.5 step 2).

    It fails against a kernel claim or another O_EXCL opener -- and NOT against
    an ordinary non-exclusive opener. Asserting the wrong behaviour here would
    let someone 'fix' the test and quietly break the spec's claim.
    """
    import errno
    with loop_device(16 * 1024 * 1024) as loop:
        plain = os.open(loop.path, os.O_RDONLY)
        try:
            exclusive = os.open(loop.path, os.O_RDWR | os.O_EXCL)
            os.close(exclusive)
        except OSError as exc:
            pytest.fail(f"a plain opener must NOT cause EBUSY, got {exc}")
        finally:
            os.close(plain)

        first = os.open(loop.path, os.O_RDWR | os.O_EXCL)
        try:
            with pytest.raises(OSError) as caught:
                os.open(loop.path, os.O_RDWR | os.O_EXCL)
            assert caught.value.errno == errno.EBUSY
        finally:
            os.close(first)


@pytest.mark.root
@needs_losetup
def test_8_guarded_open_treats_ebusy_as_fatal():
    from driveprep import safety as S
    with loop_device(16 * 1024 * 1024) as loop:
        disk = _disk(loop.kname)
        held = os.open(loop.path, os.O_RDWR | os.O_EXCL)
        try:
            with pytest.raises(S.DeviceBusyError, match="never retried"):
                with S.guarded_open(disk, disk.identity, write=True,
                                    test_mode=True):
                    pass
        finally:
            os.close(held)


# --------------------------------------------------------------------------
# by-id selection is bus-conditional (spec 4.1)
# --------------------------------------------------------------------------


def test_by_id_preference_is_bus_conditional():
    """A USB-bridged drive routinely exposes ata-, usb- AND wwn- names.

    Found on real hardware: a WD My Book presented all three, and a flat
    priority list picked the ata- name for a USB drive -- the drive's serial
    rather than the enclosure's, at the bridge's discretion, displayed on a row
    marked "usb".
    """
    names = [
        "ata-WDC_WD5000AAVS-00ZTB0_WD-WCASU1609204",
        "usb-WD_5000AAV_External_57442D574341535531363039323034-0:0",
        "wwn-0x50014ee2abb09c73",
    ]
    assert inv.preferred_by_id(names, inv.BUS_USB).startswith("usb-")
    assert inv.preferred_by_id(names, inv.BUS_ATA).startswith("ata-")


def test_by_id_falls_back_when_the_preferred_form_is_absent():
    assert inv.preferred_by_id(
        ["wwn-0x5000", "ata-Foo_123"], inv.BUS_USB) == "ata-Foo_123"
    assert inv.preferred_by_id(["wwn-0x5000"], inv.BUS_USB) == "wwn-0x5000"
    assert inv.preferred_by_id([], inv.BUS_USB) is None


def test_by_id_ignores_partition_entries():
    """-partN entries are partitions, not disks (spec 4.1)."""
    mapping = inv.by_id_map()
    for kname, names in mapping.items():
        for name in names:
            assert not name.endswith(tuple(f"-part{i}" for i in range(1, 10)))
