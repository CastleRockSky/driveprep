"""Device enumeration (spec 4.1).

The canonical identifier for a drive is its /dev/disk/by-id/ name. Kernel names
are assigned in discovery order and move between boots and replugs, so they are
resolved only at the moment of use and re-verified per spec 4.5. Nothing in this
package persists a /dev/sdX anywhere.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import identity as ident
from . import log

_log = log.get("inventory")

BY_ID = Path("/dev/disk/by-id")
SYS_BLOCK = Path("/sys/block")

# by-id entries ending in -partN are partitions, not disks (spec 4.1).
_PART_SUFFIX = re.compile(r"-part\d+$")

# Preference order for the canonical name (spec 4.1).
_BY_ID_PREFIXES = ("ata-", "usb-", "nvme-", "scsi-", "wwn-")

BUS_USB = "usb"
BUS_ATA = "ata"
BUS_NVME = "nvme"
BUS_UNKNOWN = "unknown"


def capacity_label(size_bytes: int) -> str:
    """Capacity as a seller would advertise it: decimal, not binary.

    A '4 TB' drive is 4000787030016 bytes. Reporting '3.6 TiB' on a listing
    image invites a dispute, so the report uses the marketed figure and the
    exact byte count side by side.
    """
    if size_bytes >= 1_000_000_000_000:
        value, unit = size_bytes / 1_000_000_000_000, "TB"
    elif size_bytes >= 1_000_000_000:
        value, unit = size_bytes / 1_000_000_000, "GB"
    elif size_bytes >= 1_000_000:
        value, unit = size_bytes / 1_000_000, "MB"
    else:
        return f"{size_bytes} bytes"
    # Snap to the whole number when we are within 1% of it: a 4 TB drive is
    # 4000787030016 bytes and a 500 GB drive is 500107862016, so the marketed
    # figure is always a hair under the true decimal value.
    whole = round(value)
    if whole and abs(value - whole) / whole < 0.01:
        return f"{whole} {unit}"
    return f"{round(value, 1):g} {unit}"


@dataclass
class Disk:
    """A whole block device as seen at inventory time.

    Eligibility is *not* decided here -- safety.py owns that. This type carries
    facts; the ineligible_reasons list is filled in by the safety layer.
    """

    kname: str
    by_id: str | None
    synthetic_id: str | None
    identity: ident.Identity
    bus_type: str
    size_bytes: int
    logical_block_bytes: int
    physical_block_bytes: int
    sysfs_rotational: int | None
    read_only: bool
    device_class: str
    model: str
    serial: str
    pttype: str | None = None
    fs_labels: list[str] = field(default_factory=list)
    partitions: list[str] = field(default_factory=list)
    usb_speed_mbps: float | None = None
    ineligible_reasons: list[str] = field(default_factory=list)
    # Filled in by the pre-gate phases so the manifest can show them.
    short_test_status: str | None = None
    skip_erase: bool = False

    @property
    def id(self) -> str:
        """Stable identifier used for the output directory and the token."""
        return self.by_id or self.synthetic_id or self.kname

    @property
    def output_name(self) -> str:
        """Filesystem-safe form of the identifier."""
        return re.sub(r"[^A-Za-z0-9._-]", "_", self.id)

    @property
    def dev_path(self) -> str:
        """Path to open. Prefer by-id; it is what spec 4.5 step 1 asks for."""
        if self.by_id:
            return str(BY_ID / self.by_id)
        return f"/dev/{self.kname}"

    @property
    def eligible(self) -> bool:
        return not self.ineligible_reasons

    @property
    def is_internal_bus(self) -> bool:
        """Anything not USB gets the [INTERNAL BUS] tag in the manifest."""
        return self.bus_type != BUS_USB

    @property
    def capacity_label(self) -> str:
        return capacity_label(self.size_bytes)

    def __str__(self) -> str:
        return f"{self.id} ({self.kname}, {self.capacity_label})"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return None


def _read_int(path: Path, default: int | None = None) -> int | None:
    raw = _read(path)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def by_id_map() -> dict[str, list[str]]:
    """kernel name -> by-id names, partitions excluded."""
    mapping: dict[str, list[str]] = {}
    if not BY_ID.is_dir():
        return mapping
    for entry in sorted(BY_ID.iterdir()):
        if _PART_SUFFIX.search(entry.name):
            continue
        try:
            kname = Path(entry.resolve()).name
        except OSError:
            continue
        mapping.setdefault(kname, []).append(entry.name)
    return mapping


def preferred_by_id(names: list[str], bus_type: str | None = None) -> str | None:
    """Pick the canonical by-id name (spec 4.1).

    The preference is BUS-CONDITIONAL -- "ata-... for SATA, usb-... for USB" --
    not a flat priority list. A USB-bridged drive routinely exposes all three
    forms, because udev's ata_id can see through a bridge that passes ATA
    IDENTIFY. Taking ata- unconditionally then picks the wrong one:

      * it carries the DRIVE's serial, while section 4.1 says only the
        enclosure-level identity is used for selection;
      * it exists only at the bridge's discretion, so an identifier meant to be
        stable would vary with bridge behaviour -- exactly what by-id exists to
        prevent;
      * and the operator sees an `ata-` name on a row marked `usb` at the
        confirmation gate, which is a bad way to identify a drive you are about
        to destroy.
    """
    if bus_type == BUS_USB:
        order = ("usb-", "ata-", "scsi-", "wwn-")
    elif bus_type == BUS_ATA:
        order = ("ata-", "scsi-", "usb-", "wwn-")
    else:
        order = _BY_ID_PREFIXES

    for prefix in order:
        for name in sorted(names):
            if name.startswith(prefix):
                return name
    return sorted(names)[0] if names else None


def synthetic_id(model: str, serial: str, size_bytes: int) -> str:
    """Fallback identifier for a drive with no usable by-id entry (spec 4.1).

    Also covers every --test-mode loop and dm fixture, which is why the
    confirmation token in spec 4.4 is well defined for test runs too.
    """
    digest = hashlib.sha1(f"{model}{serial}{size_bytes}".encode()).hexdigest()
    return f"dp-{digest[:12]}"


def detect_bus_type(kname: str, device_class: str) -> str:
    """Bus type is informational, never a gate (spec 4.3)."""
    if kname.startswith("nvme"):
        return BUS_NVME
    if device_class != ident.CLASS_SCSI:
        return BUS_UNKNOWN
    try:
        target = (SYS_BLOCK / kname / "device").resolve().as_posix()
    except OSError:
        return BUS_UNKNOWN
    if "/usb" in target:
        return BUS_USB
    if "/ata" in target:
        return BUS_ATA
    # Some SATA controllers present through libata without 'ata' in the path;
    # fall back to lsblk's transport, which handles the odd HBA.
    return BUS_UNKNOWN


def usb_link_speed(kname: str) -> float | None:
    """Negotiated USB link speed in Mbps, for the Appendix B slow-link warning.

    Walks up from the block device to the nearest USB device node carrying a
    'speed' attribute.
    """
    try:
        node = (SYS_BLOCK / kname / "device").resolve()
    except OSError:
        return None
    for parent in [node, *node.parents]:
        if parent.as_posix() in ("/", "/sys", "/sys/devices"):
            break
        speed = _read(parent / "speed")
        if speed:
            try:
                return float(speed)
            except ValueError:
                continue
    return None


def usb_root_hub(kname: str) -> str | None:
    """Identify the USB host controller a drive hangs off (spec 8.1).

    Used to warn when more than four drives share one controller.
    """
    try:
        node = (SYS_BLOCK / kname / "device").resolve()
    except OSError:
        return None
    match = re.search(r"/(usb\d+)/", node.as_posix() + "/")
    return match.group(1) if match else None


def _lsblk() -> dict[str, dict]:
    """Informational fields from lsblk, keyed by kernel name.

    lsblk is used only for display data -- partition table type, filesystem
    labels, transport. It is deliberately NOT used for the parent-disk walk in
    safety.py, because PKNAME is single-valued and silently wrong for
    multi-parent devices (spec 4.2).
    """
    cmd = [
        "lsblk", "-J", "-b", "-o",
        "KNAME,TYPE,PKNAME,TRAN,PTTYPE,FSTYPE,LABEL,SERIAL,MODEL,ROTA,RO",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        _log.warning("lsblk unavailable or unparseable (%s); continuing without it", exc)
        return {}

    flat: dict[str, dict] = {}

    def walk(nodes):
        for node in nodes:
            if kname := node.get("kname"):
                flat[kname] = node
            walk(node.get("children") or [])

    walk(data.get("blockdevices") or [])
    return flat


def partitions_of(kname: str) -> list[str]:
    """Partition kernel names belonging to a disk.

    Enumerated from sysfs rather than lsblk because safety.py needs this list to
    be exhaustive -- the holders-on-a-partition check (spec 4.2) is the single
    most likely point of failure in the safety layer, and it depends entirely on
    this returning everything.
    """
    sysdir = SYS_BLOCK / kname
    if not sysdir.is_dir():
        return []
    return sorted(
        child.name for child in sysdir.iterdir()
        if child.is_dir() and (child / "partition").exists()
    )


def scan(include_all_classes: bool = True) -> list[Disk]:
    """Enumerate every whole block device in /sys/block.

    Returns facts only. Nothing here decides eligibility; safety.evaluate()
    does, so that `driveprep list` can show every disk together with the
    specific reason each one was refused.
    """
    lsblk_data = _lsblk()
    id_map = by_id_map()
    disks: list[Disk] = []

    for sysdir in sorted(SYS_BLOCK.iterdir()):
        kname = sysdir.name
        if not sysdir.is_dir():
            continue
        try:
            device_class = ident.classify(sysdir)
        except ident.IdentityError:
            # zram, md, and anything else without a class we recognise. These
            # are still surfaced, because `driveprep list` should account for
            # every device rather than quietly omitting some.
            device_class = "other"

        try:
            size = ident.size_bytes_of(sysdir)
        except ident.IdentityError as exc:
            _log.debug("skipping %s: %s", kname, exc)
            continue

        if size == 0:
            # Empty card readers and detached loop devices. Nothing to do with
            # them and they clutter the listing.
            _log.debug("skipping %s: zero length", kname)
            continue

        try:
            identity = ident.identity_of(kname)
        except ident.IdentityError as exc:
            _log.debug("no identity for %s: %s", kname, exc)
            identity = ident.Identity("other", size, kname, "")

        lsb = lsblk_data.get(kname, {})
        bus = detect_bus_type(kname, device_class)
        if bus == BUS_UNKNOWN and (tran := lsb.get("tran")):
            bus = {"usb": BUS_USB, "sata": BUS_ATA, "ata": BUS_ATA,
                   "nvme": BUS_NVME}.get(tran, BUS_UNKNOWN)

        by_id = preferred_by_id(id_map.get(kname, []), bus)
        model = identity.id_model or (lsb.get("model") or "").strip()
        serial = identity.id_serial or (lsb.get("serial") or "").strip()

        parts = partitions_of(kname)
        labels = [
            lsblk_data[p]["label"]
            for p in parts
            if lsblk_data.get(p, {}).get("label")
        ]
        if lsb.get("label"):
            labels.insert(0, lsb["label"])

        disk = Disk(
            kname=kname,
            by_id=by_id,
            synthetic_id=None if by_id else synthetic_id(model, serial, size),
            identity=identity,
            bus_type=bus,
            size_bytes=size,
            logical_block_bytes=_read_int(sysdir / "queue" / "logical_block_size", 512),
            physical_block_bytes=_read_int(sysdir / "queue" / "physical_block_size", 512),
            sysfs_rotational=_read_int(sysdir / "queue" / "rotational"),
            read_only=_read_int(sysdir / "ro", 0) == 1,
            device_class=device_class,
            model=model,
            serial=serial,
            pttype=lsb.get("pttype"),
            fs_labels=labels,
            partitions=parts,
            usb_speed_mbps=usb_link_speed(kname) if bus == BUS_USB else None,
        )
        disks.append(disk)

    _log.debug("inventory found %d devices", len(disks))
    return disks


def find_by_id(disks: list[Disk], wanted: str) -> Disk | None:
    """Resolve a --id or --device argument against the inventory."""
    for disk in disks:
        if disk.by_id == wanted or disk.id == wanted or disk.kname == wanted:
            return disk
    # --device /dev/sdX form
    name = Path(wanted).name
    for disk in disks:
        if disk.kname == name:
            return disk
    return None
