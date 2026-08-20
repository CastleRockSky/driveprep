"""Every safety check in spec section 4.

This module is the reason the tool is allowed to exist. It decides what may be
written to, and nothing outside it may open a device for writing.

Two rules govern changes here:

  1. No flag relaxes section 4.2, with the single narrowly-scoped exception of
     --test-mode (spec 4.2.1), which permits loop and dm devices and nothing
     else.
  2. A check that cannot be evaluated is a refusal, never a pass. If zpool is
     installed but errors, if fstab cannot be parsed, if sysfs reads fail --
     the drive is refused with the reason recorded. Absence of evidence of
     danger is not evidence of safety.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import identity as ident
from . import inventory as inv
from . import log

_log = log.get("safety")

SYS_CLASS_BLOCK = Path("/sys/class/block")
PROC_MOUNTINFO = Path("/proc/self/mountinfo")
PROC_SWAPS = Path("/proc/swaps")
PROC_MDSTAT = Path("/proc/mdstat")
ETC_FSTAB = Path("/etc/fstab")
ETC_CRYPTTAB = Path("/etc/crypttab")

# Mountpoints whose backing disks are never eligible, however they are
# assembled (spec 4.2).
PROTECTED_MOUNTPOINTS = ("/", "/boot", "/boot/efi", "/home")

# Heuristic vendor strings for hardware RAID volumes, which are out of scope
# (spec 2). This list is deliberately conservative: a false refusal costs the
# operator one shucked drive, a false acceptance costs them an array.
_RAID_VENDORS = ("lsi", "megaraid", "avago", "dell", "perc", "adaptec",
                 "hp", "hpe", "smartarray", "cisco", "intel raid")


class SafetyError(RuntimeError):
    """A safety invariant was violated. Always fatal for the drive."""


class DeviceBusyError(SafetyError):
    """EBUSY on open. Never retryable (spec 4.5 step 2)."""


# --------------------------------------------------------------------------
# sysfs / procfs helpers
# --------------------------------------------------------------------------


def _read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return None


def devnum(kname: str) -> tuple[int, int] | None:
    """(major, minor) for a kernel name, from /sys/class/block/<kname>/dev."""
    raw = _read(SYS_CLASS_BLOCK / kname / "dev")
    if not raw or ":" not in raw:
        return None
    major, _, minor = raw.partition(":")
    try:
        return int(major), int(minor)
    except ValueError:
        return None


def is_partition(kname: str) -> bool:
    return (SYS_CLASS_BLOCK / kname / "partition").exists()


def parent_disk(kname: str) -> str:
    """Map a partition kernel name back to its whole disk.

    /proc/mdstat lists PARTITION names (sdb1[0]), so every mdstat member has to
    come through here before it can be compared against a disk (spec 4.2).
    """
    if not is_partition(kname):
        return kname
    try:
        return Path(os.path.realpath(SYS_CLASS_BLOCK / kname)).parent.name
    except OSError:
        return kname


def leaf_disks(kname: str, _seen: set[str] | None = None) -> set[str]:
    """Recurse slaves/ down to every leaf physical disk (spec 4.2).

    Do NOT replace this with `lsblk -no PKNAME`. PKNAME is single-valued, so a
    mirrored root on md RAID1 resolves to one leg and leaves the other
    eligible, and a striped LV over N PVs protects exactly one of them. It is
    also empty when queried against a dm or md node directly. A single-valued
    column cannot express a multi-parent device; this recursion can.
    """
    _seen = _seen if _seen is not None else set()
    if kname in _seen:
        return set()
    _seen.add(kname)

    slaves_dir = SYS_CLASS_BLOCK / kname / "slaves"
    slaves = []
    if slaves_dir.is_dir():
        try:
            slaves = [p.name for p in slaves_dir.iterdir()]
        except OSError as exc:
            raise SafetyError(
                f"cannot enumerate {slaves_dir}: {exc}. Refusing to guess at the "
                f"device topology."
            ) from exc

    if not slaves:
        return {parent_disk(kname)}

    out: set[str] = set()
    for slave in slaves:
        out |= leaf_disks(slave, _seen)
    return out


def resolve_to_kname(spec: str) -> str | None:
    """Resolve a device path or tag to a kernel name.

    Handles /dev/sdX, /dev/disk/by-*/..., /dev/mapper/..., and the UUID=,
    LABEL=, PARTUUID=, PARTLABEL= forms used in fstab and crypttab.
    """
    spec = spec.strip()
    if not spec:
        return None

    tag_dirs = {
        "UUID": "/dev/disk/by-uuid",
        "LABEL": "/dev/disk/by-label",
        "PARTUUID": "/dev/disk/by-partuuid",
        "PARTLABEL": "/dev/disk/by-partlabel",
        "ID": "/dev/disk/by-id",
    }
    if "=" in spec:
        tag, _, value = spec.partition("=")
        tag = tag.upper().strip()
        if tag in tag_dirs:
            candidate = Path(tag_dirs[tag]) / value.strip().strip('"')
            try:
                return Path(os.path.realpath(candidate)).name
            except OSError:
                pass
            # by-* symlink absent: the filesystem may simply not be present
            # right now, which is exactly the idle-internal-disk case spec 4.2
            # cares about. Fall through to blkid.
            return _blkid_lookup(tag, value.strip().strip('"'))
        return None

    if spec.startswith("/dev/"):
        try:
            return Path(os.path.realpath(spec)).name
        except OSError:
            return None
    return None


def _blkid_lookup(tag: str, value: str) -> str | None:
    """Fallback resolution for an fstab tag with no by-* symlink present."""
    try:
        proc = subprocess.run(
            ["blkid", "-t", f"{tag}={value}", "-o", "device"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = (proc.stdout or "").strip().splitlines()
    if not first:
        return None
    try:
        return Path(os.path.realpath(first[0])).name
    except OSError:
        return None


# --------------------------------------------------------------------------
# Individual eligibility checks. Each returns a reason string, or None.
# --------------------------------------------------------------------------


def _disk_and_partitions(disk: inv.Disk) -> list[str]:
    return [disk.kname, *disk.partitions]


def check_structural(disk: inv.Disk, test_mode: bool) -> list[str]:
    reasons = []

    if disk.kname.startswith("nvme"):
        reasons.append(
            "NVMe device. NVMe is out of scope: it needs a different "
            "sanitization method (nvme format / sanitize), not a zero fill."
        )
    if disk.kname.startswith("zram"):
        reasons.append("zram device, not a physical disk.")
    if disk.kname.startswith("md"):
        reasons.append("md (software RAID) device, not a whole physical disk.")
    if is_partition(disk.kname):
        reasons.append("not a whole disk (this is a partition).")

    if disk.device_class == ident.CLASS_LOOP and not test_mode:
        reasons.append("loop device. Only permitted under --test-mode (spec 4.2.1).")
    if disk.device_class == ident.CLASS_DM and not test_mode:
        reasons.append(
            "device-mapper target. Only permitted under --test-mode (spec 4.2.1)."
        )
    if disk.device_class == "other":
        reasons.append(
            f"unrecognised device class for {disk.kname}; DrivePrep cannot "
            f"establish an identity for it."
        )
    return reasons


def check_sas_and_raid(disk: inv.Disk) -> list[str]:
    """SAS and hardware RAID volumes are out of scope (spec 2)."""
    reasons = []
    if disk.device_class != ident.CLASS_SCSI:
        return reasons

    try:
        target = (Path("/sys/block") / disk.kname / "device").resolve().as_posix()
    except OSError:
        target = ""
    if "end_device-" in target or "/sas_" in target:
        reasons.append("SAS device. SAS is out of scope for this tool.")

    vendor = (_read(Path("/sys/block") / disk.kname / "device" / "vendor") or "").lower()
    model = (disk.model or "").lower()
    for needle in _RAID_VENDORS:
        if needle in vendor or needle in model:
            reasons.append(
                f"looks like a hardware RAID volume (vendor/model contains "
                f"{needle!r}). Hardware RAID is out of scope; erase the member "
                f"disks directly instead."
            )
            break
    return reasons


def check_read_only(disk: inv.Disk) -> list[str]:
    if disk.read_only:
        return [f"device is read-only (/sys/block/{disk.kname}/ro is 1)."]
    try:
        proc = subprocess.run(
            ["blockdev", "--getro", f"/dev/{disk.kname}"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip() == "1":
            return ["device is read-only (blockdev --getro reports 1)."]
    except (OSError, subprocess.SubprocessError) as exc:
        _log.debug("blockdev --getro failed for %s: %s", disk.kname, exc)
    return []


def check_holders(disk: inv.Disk) -> list[str]:
    """Holders on the disk OR on any of its partitions (spec 4.2).

    This is the check most likely to be built wrong. /sys/block/sdb/holders/ is
    EMPTY when the LVM PV, mdraid member, or dm-crypt backing device is sdb1
    rather than sdb -- the holder symlink lives at
    /sys/class/block/sdb1/holders/. Checking only the whole disk lets an in-use
    data disk sail through eligibility and get zeroed.
    """
    reasons = []
    for kname in _disk_and_partitions(disk):
        holders_dir = SYS_CLASS_BLOCK / kname / "holders"
        if not holders_dir.is_dir():
            continue
        try:
            holders = sorted(p.name for p in holders_dir.iterdir())
        except OSError as exc:
            reasons.append(f"cannot read {holders_dir} ({exc}); refusing.")
            continue
        if holders:
            where = "disk" if kname == disk.kname else f"partition {kname}"
            reasons.append(
                f"in use: {where} has holders {', '.join(holders)} "
                f"(LVM physical volume, mdraid member, or dm-crypt backing device)."
            )
    return reasons


# Filesystem/container signatures meaning "this device is a member of a larger
# storage structure". A plain filesystem is fine -- a drive to sell will have
# one -- but these mean something else owns the device.
_MEMBER_SIGNATURES = {
    "LVM2_member": "an LVM physical volume",
    "LVM1_member": "an LVM physical volume",
    "linux_raid_member": "a Linux software RAID member",
    "crypto_LUKS": "a LUKS encrypted container",
    "zfs_member": "a ZFS pool member",
    "bcache": "a bcache backing or cache device",
    "DDF_raid_member": "a DDF firmware RAID member",
    "isw_raid_member": "an Intel firmware RAID member",
    "ceph_bluestore": "a Ceph OSD",
}


def check_storage_signatures(disk: inv.Disk) -> list[str]:
    """Refuse a member of a storage stack even when it is not assembled.

    The holders check only sees an ACTIVE stack. A volume group that has been
    deactivated -- `vgchange -an`, a disk pulled from another machine, a boot
    where the VG was not brought up -- has an empty holders/ directory on every
    one of its PVs, is not mounted, is not in mdstat, and its fstab entry
    (/dev/vg/lv) does not resolve because the LV node does not exist. It passes
    every other rule in section 4.2 and would be zeroed, destroying the whole
    volume group.

    The on-disk signature survives deactivation, so it is what catches this.
    The same applies to an unassembled md member and an unopened LUKS
    container.
    """
    reasons = []
    for kname in _disk_and_partitions(disk):
        try:
            proc = subprocess.run(
                ["blkid", "-p", "-s", "TYPE", "-o", "value", f"/dev/{kname}"],
                capture_output=True, text=True, timeout=20, check=False,
            )
        except FileNotFoundError:
            return [
                "blkid is not available, so DrivePrep cannot tell whether this "
                "device is an unassembled LVM, RAID or LUKS member. Refusing "
                "rather than guessing. Install util-linux."
            ]
        except (OSError, subprocess.SubprocessError) as exc:
            reasons.append(f"cannot probe {kname} with blkid ({exc}); refusing.")
            continue

        # rc 2 simply means "no signature found", which is a normal, safe result.
        if proc.returncode not in (0, 2):
            reasons.append(
                f"blkid failed on {kname} (exit {proc.returncode}); refusing "
                f"rather than assuming the device is unused."
            )
            continue

        signature = (proc.stdout or "").strip()
        if signature in _MEMBER_SIGNATURES:
            where = "disk" if kname == disk.kname else f"partition {kname}"
            reasons.append(
                f"in use: {where} carries a {signature} signature, meaning it is "
                f"{_MEMBER_SIGNATURES[signature]}. This is detected even when "
                f"the volume group or array is not currently active, which is "
                f"exactly the case the holders check cannot see. If you really "
                f"mean to erase it, remove it from its group or array first."
            )
    return reasons


def check_mdstat(disk: inv.Disk) -> list[str]:
    """Membership in a software RAID array (spec 4.2).

    mdstat lists PARTITION names (sdb1[0]), so each member is mapped back to
    its parent disk before comparison.
    """
    content = _read(PROC_MDSTAT)
    if content is None:
        return []
    ours = set(_disk_and_partitions(disk))
    reasons = []
    for line in content.splitlines():
        if not line or line[0].isspace() or ":" not in line:
            continue
        array, _, rest = line.partition(":")
        array = array.strip()
        for token in rest.split():
            member = re.sub(r"\[\d+\]$", "", token)
            member = re.sub(r"\((S|F|W)\)$", "", member)
            if not member or member in ("active", "inactive", "auto-read-only"):
                continue
            if member.startswith("raid") or member.startswith("linear"):
                continue
            if member in ours or parent_disk(member) == disk.kname:
                reasons.append(
                    f"member of software RAID array {array} (listed as {member})."
                )
    return reasons


def check_zpool(disk: inv.Disk) -> list[str]:
    """ZFS vdev membership, when zpool is installed (spec 4.2)."""
    try:
        proc = subprocess.run(
            ["zpool", "status", "-P"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except FileNotFoundError:
        return []  # ZFS not installed; nothing to check.
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"zpool is installed but could not be queried ({exc}); refusing."]

    if proc.returncode != 0 and "no pools available" in (proc.stdout + proc.stderr).lower():
        return []

    ours = set(_disk_and_partitions(disk))
    reasons = []
    for line in (proc.stdout or "").splitlines():
        for token in line.split():
            if not token.startswith("/dev/"):
                continue
            try:
                kname = Path(os.path.realpath(token)).name
            except OSError:
                continue
            if kname in ours or parent_disk(kname) == disk.kname:
                reasons.append(f"in use as a ZFS vdev ({token}).")
    return reasons


def check_mounted(disk: inv.Disk) -> list[str]:
    """Mounted disk or partition (spec 4.2).

    Compared on major:minor from mountinfo field 3, not on the source path,
    because the source can be a bind mount, a by-id path, or a stale name.
    """
    content = _read(PROC_MOUNTINFO)
    if content is None:
        return ["cannot read /proc/self/mountinfo; refusing."]

    ours = {devnum(k): k for k in _disk_and_partitions(disk)}
    ours.pop(None, None)
    reasons = []
    for line in content.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        majmin = fields[2]
        try:
            major, _, minor = majmin.partition(":")
            key = (int(major), int(minor))
        except ValueError:
            continue
        if key in ours:
            mountpoint = fields[4]
            kname = ours[key]
            where = "disk" if kname == disk.kname else f"partition {kname}"
            reasons.append(
                f"mounted: {where} is mounted at {mountpoint}. DrivePrep never "
                f"unmounts anything (spec 5). Unmount it yourself and re-run if "
                f"you really mean to erase this drive."
            )
    if reasons and _udisks_running():
        reasons.append(
            "note: udisks2 is running and auto-mounts drives on plug-in, which "
            "is the usual cause of this. On a dedicated box: "
            "systemctl mask --now udisks2"
        )
    return reasons


def _udisks_running() -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", "--quiet", "udisks2"],
            capture_output=True, timeout=10, check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def check_swap(disk: inv.Disk) -> list[str]:
    content = _read(PROC_SWAPS)
    if content is None:
        return []
    ours = {devnum(k) for k in _disk_and_partitions(disk)} - {None}
    reasons = []
    for line in content.splitlines()[1:]:
        path = line.split()[0] if line.split() else ""
        if not path.startswith("/dev/"):
            continue
        try:
            st = os.stat(path)
            key = (os.major(st.st_rdev), os.minor(st.st_rdev))
        except OSError:
            continue
        if key in ours:
            reasons.append(
                f"active swap area ({path}). DrivePrep never calls swapoff "
                f"(spec 5); do it yourself and re-run."
            )
    return reasons


def _fstab_like_specs(path: Path) -> list[str]:
    content = _read(path)
    if content is None:
        return []
    specs = []
    for line in content.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if not fields:
            continue
        # fstab: first field is the device. crypttab: second field is.
        if path == ETC_CRYPTTAB and len(fields) >= 2:
            specs.append(fields[1])
        else:
            specs.append(fields[0])
    return specs


def check_fstab_crypttab(disk: inv.Disk) -> list[str]:
    """Referenced in /etc/fstab or /etc/crypttab (spec 4.2).

    An idle internal data disk that is in fstab but happens not to be mounted
    right now passes every "currently active" check. With SATA a first-class
    path, this is the most realistic remaining wrong-device scenario in the
    whole design.
    """
    reasons = []
    ours = set(_disk_and_partitions(disk))
    for path, label in ((ETC_FSTAB, "fstab"), (ETC_CRYPTTAB, "crypttab")):
        for spec in _fstab_like_specs(path):
            kname = resolve_to_kname(spec)
            if not kname:
                continue
            if kname in ours or parent_disk(kname) == disk.kname:
                reasons.append(
                    f"referenced in /etc/{label} as {spec!r} (resolves to "
                    f"{kname}). The system is configured to use this device, "
                    f"whether or not it happens to be mounted right now."
                )
    return reasons


def protected_disks(output_root: Path | None = None) -> set[str]:
    """Every leaf disk backing a protected mountpoint or the output directory."""
    protected: set[str] = set()
    targets = list(PROTECTED_MOUNTPOINTS)
    if output_root is not None:
        targets.append(str(output_root))

    for target in targets:
        if not Path(target).exists():
            continue
        try:
            proc = subprocess.run(
                ["findmnt", "-no", "SOURCE", "--target", str(target)],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SafetyError(
                f"cannot resolve the device backing {target} ({exc}). Refusing "
                f"to run without knowing which disk holds the system."
            ) from exc
        source = (proc.stdout or "").strip().splitlines()
        if not source:
            continue
        kname = resolve_to_kname(source[0].split("[")[0])
        if not kname:
            continue
        protected |= leaf_disks(kname)
    return protected


def check_system_disk(disk: inv.Disk, protected: set[str]) -> list[str]:
    if disk.kname in protected:
        return [
            f"backs the running system or the output directory "
            f"({disk.kname} is a leaf of /, /boot, /boot/efi, /home, or "
            f"--output-root)."
        ]
    return []


def check_media_type(disk: inv.Disk, smart_info: dict | None) -> list[str]:
    """Solid state refusal (spec 4.2).

    Stated against smartctl's JSON, because that is the input:

        rotation_rate present and == 0  -> solid state -> INELIGIBLE
        rotation_rate present and  > 0  -> spinning, record the RPM
        rotation_rate absent            -> unknown -> ELIGIBLE, rpm null

    Note this does NOT mirror ATA IDENTIFY word 217, where 0x0001 means
    non-rotating and 0x0000 means not reported. smartctl inverts it. Refusal
    requires positive evidence of solid state, never absence of evidence of
    rotation -- writing it the other way would make every SMART-blocked bridge
    and every test loop device unreachable.

    /sys/block/<k>/queue/rotational is advisory only and frequently wrong for
    USB: many bridges report 1 for everything including SSDs, and a few report
    0 for spinning drives. It is logged and displayed, never decisive.
    """
    if smart_info is None:
        return []

    rate = smart_info.get("rotation_rate")
    if rate is not None and int(rate) == 0:
        return [
            "solid state device (smartctl reports rotation_rate 0). SSDs are "
            "out of scope: a zero fill is the wrong sanitization method for "
            "flash. Use the drive's own secure-erase or crypto-erase."
        ]

    text = (smart_info.get("_text") or "")
    if "solid state device" in text.lower():
        return [
            "solid state device (smartctl text reports 'Rotation Rate: Solid "
            "State Device'). SSDs are out of scope."
        ]

    if rate is not None and disk.sysfs_rotational == 0 and int(rate) > 0:
        _log.info(
            "%s: sysfs rotational=0 but smartctl reports %s RPM; trusting "
            "smartctl and recording the discrepancy", disk.kname, rate,
        )
    return []


# --------------------------------------------------------------------------
# Aggregate evaluation
# --------------------------------------------------------------------------


def evaluate(
    disk: inv.Disk,
    *,
    test_mode: bool = False,
    usb_only: bool = False,
    output_root: Path | None = None,
    smart_info: dict | None = None,
    protected: set[str] | None = None,
) -> list[str]:
    """Run every spec 4.2 check. Returns the list of refusal reasons.

    An empty list means eligible. The disk's own ineligible_reasons is updated
    in place so `driveprep list` can print per-disk reasons.
    """
    if protected is None:
        protected = protected_disks(output_root)

    reasons: list[str] = []
    reasons += check_structural(disk, test_mode)
    reasons += check_sas_and_raid(disk)
    reasons += check_read_only(disk)
    reasons += check_holders(disk)
    reasons += check_storage_signatures(disk)
    reasons += check_mdstat(disk)
    reasons += check_zpool(disk)
    reasons += check_mounted(disk)
    reasons += check_swap(disk)
    reasons += check_fstab_crypttab(disk)
    reasons += check_system_disk(disk, protected)
    reasons += check_media_type(disk, smart_info)

    if usb_only and disk.bus_type != inv.BUS_USB:
        reasons.append(
            f"--usb-only was given and this drive is on the {disk.bus_type} bus."
        )

    if test_mode and disk.device_class not in ident.TEST_MODE_CLASSES:
        reasons.append(
            f"--test-mode permits loop and dm devices only; {disk.kname} is a "
            f"{disk.device_class} device. Refusing to run."
        )

    if not test_mode and not disk.by_id:
        reasons.append(
            "no /dev/disk/by-id entry. This drive cannot be safely "
            "re-identified after a replug; select it explicitly with "
            "--device /dev/" + disk.kname + " and note it may not be used in "
            "queue mode."
        )

    disk.ineligible_reasons = reasons
    return reasons


# --------------------------------------------------------------------------
# Confirmation gate (spec 4.4)
# --------------------------------------------------------------------------


def identity_string(disk: inv.Disk) -> str:
    """Per-drive identity string used to derive the token (spec 4.4).

    In order of preference: the by-id name, else the synthetic identifier,
    which covers --device drives and every --test-mode fixture. Both forms are
    stable for the lifetime of a batch.
    """
    return disk.by_id or disk.synthetic_id or disk.kname


def compute_token(disks: list[inv.Disk]) -> str:
    """DP-<N>-<first 4 hex of SHA1 of the sorted identity strings>.

    Computed over the PHASE-0 ELIGIBLE SET -- the drives that passed section
    4.2 at inventory -- not over the set that survives the phase-2 short self
    test. The token's job is to detect a change in which devices are attached
    between planning and confirming. Deriving it from the post-self-test set
    would make it depend on drive health, which is not stable across runs: a
    drive whose short test passes during planning and fails during execution
    would change the token and abort the whole batch, defeating the
    --confirm-token unattended path entirely.

    The hash input is pinned so the token is reproducible across versions.
    """
    ids = sorted(identity_string(d) for d in disks)
    payload = "\n".join(ids).encode("utf-8")
    digest = hashlib.sha1(payload).hexdigest().upper()
    return f"DP-{len(ids)}-{digest[:4]}"


def verify_token(supplied: str, expected: str) -> bool:
    """Case-insensitive, whitespace-trimmed comparison."""
    return supplied.strip().casefold() == expected.strip().casefold()


@dataclass
class Manifest:
    disks: list[inv.Disk]
    token: str
    total_bytes: int
    estimate_seconds: float | None

    def render(self, plan_mode: bool = False) -> str:
        lines = []
        lines.append("")
        lines.append("=" * 78)
        lines.append("  DRIVES TO BE ERASED -- ALL DATA ON THESE DEVICES WILL BE DESTROYED")
        lines.append("=" * 78)
        for disk in self.disks:
            tag = "  [INTERNAL BUS]" if disk.is_internal_bus else ""
            lines.append("")
            lines.append(f"  {disk.id}{tag}")
            lines.append(f"    kernel name      {disk.kname}")
            lines.append(f"    model            {disk.model or '(unknown)'}")
            lines.append(f"    enclosure serial {disk.serial or '(unknown)'}")
            ata = getattr(disk, "ata_serial", None)
            if ata and ata != disk.serial:
                lines.append(f"    ATA serial       {ata}")
            lines.append(
                f"    capacity         {disk.capacity_label} "
                f"({disk.size_bytes:,} bytes)"
            )
            lines.append(f"    bus              {disk.bus_type}")
            lines.append(
                f"    block size       {disk.logical_block_bytes} logical / "
                f"{disk.physical_block_bytes} physical"
            )
            lines.append(f"    partition table  {disk.pttype or '(none)'}")
            if disk.fs_labels:
                lines.append(f"    filesystem labels {', '.join(disk.fs_labels)}")
            lines.append(f"    will overwrite   {disk.size_bytes:,} bytes")
            if disk.short_test_status:
                lines.append(f"    short test       {disk.short_test_status}")
            elif plan_mode:
                lines.append("    short test       not run (plan mode)")
            if disk.usb_speed_mbps and disk.usb_speed_mbps <= 480:
                lines.append(
                    f"    WARNING          negotiated {disk.usb_speed_mbps:g} Mbps "
                    f"(USB 2.0 or slower); expect roughly 4x the estimated time"
                )
        lines.append("")
        lines.append("-" * 78)
        lines.append(f"  {len(self.disks)} drive(s), {self.total_bytes:,} bytes total")
        if self.estimate_seconds:
            # A range when concurrent drives contend for the bus, because the
            # honest answer is an interval rather than a false-precision point.
            if isinstance(self.estimate_seconds, tuple):
                low, high = (v / 3600 for v in self.estimate_seconds)
                lines.append(
                    f"  estimated wall-clock: {low:.1f} to {high:.1f} hours")
            else:
                hours = self.estimate_seconds / 3600
                lines.append(f"  estimated wall-clock: {hours:.1f} hours")
        lines.append("-" * 78)
        return "\n".join(lines)


def confirm(
    manifest: Manifest,
    *,
    execute: bool,
    supplied_token: str | None,
    stream_in=None,
) -> bool:
    """The spec 4.4 gate. Returns True only if every step passes.

    There is no flag that skips confirmation. `resume` goes through this
    identical gate with a token recomputed from the drives it is about to
    resume.
    """
    print(manifest.render(plan_mode=not execute))
    print()
    print(f"  Confirmation token: {manifest.token}")
    print()

    if not execute:
        print("  Plan mode: --execute was not given, so nothing will be written.")
        print("  Re-run with --execute to proceed.")
        return False

    if supplied_token is not None:
        if not verify_token(supplied_token, manifest.token):
            print(
                f"  REFUSED: supplied token {supplied_token!r} does not match "
                f"{manifest.token}.\n"
                f"  The set of attached drives has changed since that token was "
                f"computed.\n  Re-run without --confirm-token to see the current "
                f"manifest."
            )
            return False
        print("  Token supplied on the command line matches. Proceeding.")
        return True

    prompt = f"  Type the token exactly to proceed (anything else aborts): "
    try:
        if stream_in is not None:
            answer = stream_in.readline()
        else:
            answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        return False

    if verify_token(answer, manifest.token):
        return True
    print("  Token did not match. Aborted. Nothing was written.")
    return False


# --------------------------------------------------------------------------
# Guarded device open (spec 4.5)
# --------------------------------------------------------------------------


def _recheck_eligibility(kname: str, test_mode: bool, output_root: Path | None) -> None:
    """Re-run the COMPLETE spec 4.2 rule set after open (spec 4.5 step 4).

    Not a subset. Hours pass between phase 0 and phase 4 -- short self-tests
    plus operator confirmation -- and every check here is a cheap sysfs or
    procfs read. A disk that gained an LVM PV, was added to an md array, or was
    written into fstab during the confirmation window is caught here and
    nowhere else.
    """
    fresh = None
    for disk in inv.scan():
        if disk.kname == kname:
            fresh = disk
            break
    if fresh is None:
        raise SafetyError(
            f"{kname} disappeared from the inventory between open and re-check."
        )
    reasons = evaluate(fresh, test_mode=test_mode, output_root=output_root)
    if reasons:
        raise SafetyError(
            f"{kname} became ineligible after the device was opened: "
            + "; ".join(reasons)
        )


@contextmanager
def guarded_open(
    disk: inv.Disk,
    recorded: ident.Identity,
    *,
    write: bool,
    test_mode: bool = False,
    output_root: Path | None = None,
):
    """Open a device for I/O, running the full spec 4.5 sequence.

    Yields (fd, kernel_name). Nothing else in this package may open a block
    device for writing.

    Sequence:
      1. open the by-id path directly (not a resolved /dev/sdX)
      2. treat EBUSY as a fatal abort, never retryable
      3. recompute the identity tuple from the OPEN descriptor and compare
      4. re-run the full section 4.2 rule set against the resolved kernel name

    Step 3 is the check that matters, because it interrogates the object that
    is about to be written rather than a name that may since have been
    reassigned. Step 1's use of by-id is a convenience, not the security
    boundary.
    """
    flags = os.O_DIRECT | os.O_EXCL | (os.O_RDWR if write else os.O_RDONLY)
    path = disk.dev_path

    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.EBUSY:
            raise DeviceBusyError(
                f"{path}: EBUSY. Something holds a kernel claim on this device "
                f"(a mount, an md or dm target, a swap area) or another "
                f"DrivePrep instance has it open. This is never retried."
            ) from exc
        raise SafetyError(f"cannot open {path}: {exc}") from exc

    try:
        kname = ident.assert_matches(recorded, fd)
        _recheck_eligibility(kname, test_mode, output_root)
        _log.debug("guarded open ok: %s -> %s (write=%s)", path, kname, write)
        yield fd, kname
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def reread_partition_table(disk: inv.Disk) -> None:
    """Tell the kernel to drop the erased partition table (spec 5).

    Order matters and the obvious order fails: BLKRRPART needs an exclusive
    claim, so it returns EBUSY for as long as the caller still holds the
    O_EXCL descriptor. Call this only AFTER that descriptor is closed.

    Non-fatal on failure -- the erase already succeeded, and a stale in-kernel
    partition table does not affect the verify, which addresses the whole-disk
    device by offset.
    """
    BLKRRPART = 0x125F
    try:
        fd = os.open(disk.dev_path, os.O_RDONLY)
    except OSError as exc:
        _log.warning("cannot reopen %s to re-read partition table: %s",
                     disk.dev_path, exc)
        return
    try:
        fcntl.ioctl(fd, BLKRRPART, 0)
        _log.info("%s: kernel re-read the (now empty) partition table", disk.id)
    except OSError as exc:
        _log.warning(
            "%s: BLKRRPART failed (%s). Harmless -- the erase succeeded and the "
            "verify addresses the device by offset.", disk.id, exc,
        )
    finally:
        os.close(fd)
