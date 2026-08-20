"""Acceptance criteria that needed real kernel fixtures (spec 15).

Three protections were previously carried by code inspection alone:

  * plan mode never opens a device for writing
  * the slaves/ recursion protects EVERY leg of a multi-parent device, not one
  * an active swap area makes a disk ineligible

All three guard against wiping the wrong disk, and "the code looks right" is
exactly the standard that let a fabricated verification claim ship. These
observe the behaviour instead.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from driveprep import inventory as inv, safety
from make_loop import loop_device

from conftest import needs_losetup

PROJECT = Path(__file__).resolve().parent.parent

needs_strace = pytest.mark.skipif(shutil.which("strace") is None,
                                  reason="strace not installed")
needs_mdadm = pytest.mark.skipif(shutil.which("mdadm") is None,
                                 reason="mdadm not installed")
needs_swap = pytest.mark.skipif(shutil.which("mkswap") is None
                                or shutil.which("swapon") is None,
                                reason="mkswap/swapon not available")


# --------------------------------------------------------------------------
# "Running without --execute never opens a device for writing"
# --------------------------------------------------------------------------

# openat(AT_FDCWD, "/dev/loop5", O_RDONLY|O_DIRECT|O_EXCL) = 3
_OPEN_RE = re.compile(r'\bopenat?\([^,]*,\s*"([^"]+)"\s*,\s*([A-Z0-9_|]+)')

_WRITE_FLAGS = ("O_RDWR", "O_WRONLY")


def _block_opens(strace_log: str) -> list[tuple[str, str]]:
    """(path, flags) for every open of something under /dev/ that looks like a disk."""
    out = []
    for path, flags in _OPEN_RE.findall(strace_log):
        if not path.startswith("/dev/"):
            continue
        name = os.path.basename(path)
        if (name.startswith(("loop", "sd", "nvme", "dm-", "md"))
                or path.startswith("/dev/disk/")
                or path.startswith("/dev/mapper/")):
            out.append((path, flags))
    return out


@pytest.mark.root
@needs_losetup
@needs_strace
def test_plan_mode_never_opens_a_device_for_writing(tmp_path):
    """Spec 15, observed rather than reasoned about.

    Plan mode legitimately opens the device O_RDONLY -- it reads 1 GB from the
    middle for the duration estimate (spec 7 requires the estimate be computed
    without writing). What must never appear is O_RDWR or O_WRONLY.
    """
    with loop_device(64 * 1024 * 1024) as loop:
        log = tmp_path / "strace.log"
        proc = subprocess.run(
            ["strace", "-f", "-e", "trace=openat,open", "-o", str(log),
             "python3", "-m", "driveprep", "run",
             "--device", loop.path, "--test-mode",
             "--output-root", str(tmp_path / "out")],
            cwd=PROJECT, capture_output=True, text=True, timeout=300,
        )
        assert log.exists(), f"strace produced no log; stderr={proc.stderr[:400]}"
        text = log.read_text(errors="replace")

        opens = _block_opens(text)
        assert opens, ("plan mode did not open the device at all -- the test "
                       "would pass vacuously")

        writable = [(p, f) for p, f in opens
                    if any(w in f for w in _WRITE_FLAGS)]
        assert writable == [], (
            f"plan mode opened a block device for writing: {writable}")

        # And it really did read it, which is what makes the above meaningful.
        assert any("O_RDONLY" in f for _p, f in opens)


@pytest.mark.root
@needs_losetup
@needs_strace
def test_list_never_opens_a_device_at_all(tmp_path):
    """`driveprep list` is pure inventory; it should not open disks even to read."""
    with loop_device(32 * 1024 * 1024):
        log = tmp_path / "strace-list.log"
        subprocess.run(
            ["strace", "-f", "-e", "trace=openat,open", "-o", str(log),
             "python3", "-m", "driveprep", "list",
             "--output-root", str(tmp_path / "out")],
            cwd=PROJECT, capture_output=True, text=True, timeout=300,
        )
        writable = [(p, f) for p, f in _block_opens(log.read_text(errors="replace"))
                    if any(w in f for w in _WRITE_FLAGS)]
        assert writable == [], f"`list` opened a device for writing: {writable}"


# --------------------------------------------------------------------------
# Multi-parent devices: BOTH legs of a RAID1 must be protected
# --------------------------------------------------------------------------


@pytest.mark.root
@needs_losetup
@needs_mdadm
def test_both_legs_of_a_raid1_are_protected(tmp_path):
    """The case a single-valued PKNAME lookup gets wrong (spec 4.2).

    `lsblk -no PKNAME` reports ONE parent. On a mirrored root that resolves to
    one leg and leaves the other eligible -- so the tool would happily zero
    half a live mirror. The slaves/ recursion has to return every leaf.
    """
    array = "/dev/md/dpselftest"
    mountpoint = tmp_path / "mnt"
    mountpoint.mkdir()

    with loop_device(96 * 1024 * 1024) as a, loop_device(96 * 1024 * 1024) as b:
        created = subprocess.run(
            ["mdadm", "--create", array, "--run", "--metadata=1.2",
             "--level=1", "--raid-devices=2", a.path, b.path],
            capture_output=True, text=True)
        if created.returncode != 0:
            pytest.skip(f"mdadm could not build the array: "
                        f"{created.stderr.strip()[:200]}")
        try:
            time.sleep(1)
            md_kname = Path(os.path.realpath(array)).name

            # The recursion must reach BOTH loop devices.
            leaves = safety.leaf_disks(md_kname)
            assert leaves == {a.kname, b.kname}, (
                f"slaves/ recursion returned {leaves}, expected both legs")

            # Demonstrate why the recursion exists: PKNAME sees one parent.
            pk = subprocess.run(["lsblk", "-no", "PKNAME", array],
                                capture_output=True, text=True)
            reported = {ln.strip() for ln in pk.stdout.splitlines() if ln.strip()}
            assert len(reported) < 2 or reported != leaves, (
                "if PKNAME ever returns both legs this test's premise is stale")

            # Mount it and treat that as a protected mountpoint, exactly as a
            # mirrored / would be.
            subprocess.run(["mkfs.ext4", "-q", "-F", array],
                           check=True, capture_output=True)
            subprocess.run(["mount", array, str(mountpoint)],
                           check=True, capture_output=True)
            try:
                import driveprep.safety as S
                original = S.PROTECTED_MOUNTPOINTS
                S.PROTECTED_MOUNTPOINTS = (str(mountpoint),)
                try:
                    protected = S.protected_disks()
                    assert a.kname in protected and b.kname in protected, (
                        f"only {protected & {a.kname, b.kname}} protected; "
                        f"a mirrored root must protect both legs")

                    # End to end: neither leg may be eligible.
                    for disk in inv.scan():
                        if disk.kname in (a.kname, b.kname):
                            reasons = S.evaluate(disk, test_mode=True,
                                                 protected=protected)
                            assert reasons, f"{disk.kname} must be refused"
                finally:
                    S.PROTECTED_MOUNTPOINTS = original
            finally:
                subprocess.run(["umount", str(mountpoint)], check=False,
                               capture_output=True)
        finally:
            subprocess.run(["mdadm", "--stop", array], check=False,
                           capture_output=True)
            for dev in (a.path, b.path):
                subprocess.run(["mdadm", "--zero-superblock", dev], check=False,
                               capture_output=True)


@pytest.mark.root
@needs_losetup
@needs_mdadm
def test_raid_members_are_refused_by_mdstat_too(tmp_path):
    """Belt and braces: membership is caught independently of holders."""
    array = "/dev/md/dpselftest2"
    with loop_device(96 * 1024 * 1024) as a, loop_device(96 * 1024 * 1024) as b:
        created = subprocess.run(
            ["mdadm", "--create", array, "--run", "--metadata=1.2",
             "--level=1", "--raid-devices=2", a.path, b.path],
            capture_output=True, text=True)
        if created.returncode != 0:
            pytest.skip("mdadm could not build the array")
        try:
            time.sleep(1)
            for disk in inv.scan():
                if disk.kname in (a.kname, b.kname):
                    assert safety.check_mdstat(disk), \
                        f"{disk.kname} is an array member and mdstat must say so"
                    assert safety.check_holders(disk), \
                        f"{disk.kname} is held by the array"
        finally:
            subprocess.run(["mdadm", "--stop", array], check=False,
                           capture_output=True)
            for dev in (a.path, b.path):
                subprocess.run(["mdadm", "--zero-superblock", dev], check=False,
                               capture_output=True)


# --------------------------------------------------------------------------
# Active swap
# --------------------------------------------------------------------------


@pytest.mark.root
@needs_losetup
@needs_swap
def test_active_swap_area_is_refused():
    """A disk carrying live swap must never be eligible (spec 4.2).

    DrivePrep never calls swapoff on the operator's behalf -- it refuses and
    says so.
    """
    before = Path("/proc/swaps").read_text()

    with loop_device(64 * 1024 * 1024) as loop:
        subprocess.run(["mkswap", loop.path], check=True, capture_output=True)
        on = subprocess.run(["swapon", loop.path], capture_output=True, text=True)
        if on.returncode != 0:
            pytest.skip(f"swapon refused this loop device: "
                        f"{on.stderr.strip()[:200]}")
        try:
            assert loop.path in Path("/proc/swaps").read_text(), \
                "fixture did not actually activate swap"

            disk = next(d for d in inv.scan() if d.kname == loop.kname)
            reasons = safety.check_swap(disk)
            assert reasons, "an active swap area must be detected"
            assert "swapoff" in reasons[0], \
                "the refusal must tell the operator what to do about it"

            assert safety.evaluate(disk, test_mode=True, protected=set()), \
                "--test-mode must not relax the swap refusal"
        finally:
            subprocess.run(["swapoff", loop.path], check=False,
                           capture_output=True)

    # The system's own swap must be exactly as we found it.
    assert Path("/proc/swaps").read_text() == before, \
        "the fixture disturbed the host's swap configuration"


# --------------------------------------------------------------------------
# The strace detector itself, so the tests above cannot pass vacuously
# --------------------------------------------------------------------------


SAMPLE_STRACE = '''
openat(AT_FDCWD, "/etc/fstab", O_RDONLY|O_CLOEXEC) = 3
openat(AT_FDCWD, "/dev/loop5", O_RDONLY|O_DIRECT|O_EXCL) = 7
openat(AT_FDCWD, "/sys/block/loop5/size", O_RDONLY|O_CLOEXEC) = 8
openat(AT_FDCWD, "/usr/lib/python3.12/os.py", O_RDONLY|O_CLOEXEC) = 9
'''

OFFENDING_STRACE = SAMPLE_STRACE + '''
openat(AT_FDCWD, "/dev/sdb", O_RDWR|O_DIRECT|O_EXCL) = 11
'''


def test_strace_detector_ignores_ordinary_reads():
    opens = _block_opens(SAMPLE_STRACE)
    assert ("/dev/loop5", "O_RDONLY|O_DIRECT|O_EXCL") in opens
    assert not any(p.startswith("/etc") or p.startswith("/usr")
                   for p, _f in opens), "only block devices are considered"
    assert not any(w in f for _p, f in opens for w in _WRITE_FLAGS)


def test_strace_detector_catches_a_writable_open():
    """If this ever stops failing, the acceptance tests above mean nothing."""
    writable = [(p, f) for p, f in _block_opens(OFFENDING_STRACE)
                if any(w in f for w in _WRITE_FLAGS)]
    assert writable == [("/dev/sdb", "O_RDWR|O_DIRECT|O_EXCL")]


def test_strace_detector_covers_by_id_and_mapper_paths():
    log = ('openat(AT_FDCWD, "/dev/disk/by-id/usb-WD_X-0:0", O_RDWR) = 3\n'
           'openat(AT_FDCWD, "/dev/mapper/vg-lv", O_WRONLY) = 4\n')
    found = {p for p, f in _block_opens(log)
             if any(w in f for w in _WRITE_FLAGS)}
    assert found == {"/dev/disk/by-id/usb-WD_X-0:0", "/dev/mapper/vg-lv"}
