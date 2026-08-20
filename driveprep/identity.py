"""Device identity and locator tracking (spec 4.1, 4.5, 6.4).

Two distinct concepts live here and conflating them breaks reconnect handling:

  Identity  - compared for equality; a mismatch aborts the run.
  Locator   - recorded and refreshed, NEVER compared; used only to correlate
              kernel log lines to a drive.

A drive that is unplugged and replugged into a different USB port is the same
drive with a different locator. Requiring the locator to match would abort
exactly the case spec 9.1 exists to survive.

The identity tuple's *sources* depend on the device class. Loop and dm devices
have no /sys/block/<kname>/device/ directory at all -- no model, no vpd_pg80,
and udevadm reports no ID_SERIAL_SHORT for them. A single SCSI-shaped tuple is
not implementable for the section 14 fixtures, and every destructive test runs
through those.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import log

_log = log.get("identity")

SYS_BLOCK = Path("/sys/block")
SYS_DEV_BLOCK = Path("/sys/dev/block")

CLASS_SCSI = "scsi"
CLASS_LOOP = "loop"
CLASS_DM = "dm"

# Device classes that --test-mode is allowed to touch (spec 4.2.1).
TEST_MODE_CLASSES = frozenset({CLASS_LOOP, CLASS_DM})


class IdentityError(RuntimeError):
    """Identity could not be computed, or did not match what was recorded."""


def _read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except (OSError, ValueError):
        return None


def _read_int(path: Path) -> int | None:
    raw = _read(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def classify(sysdir: Path) -> str:
    """Resolve a device's class from its sysfs directory.

    Order matters: check for the class-specific subdirectory first, because
    'device' is the fallback and is the one that is absent on loop and dm.
    """
    if (sysdir / "loop").is_dir():
        return CLASS_LOOP
    if (sysdir / "dm").is_dir():
        return CLASS_DM
    if (sysdir / "device").exists():
        return CLASS_SCSI
    raise IdentityError(
        f"{sysdir.name}: cannot classify device -- no loop/, dm/, or device/ in "
        f"{sysdir}. This is not a device DrivePrep knows how to identify."
    )


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """The spec 4.5 matching fields. Compared for equality; mismatch aborts.

    `cls` participates in the comparison, so a loop device can never satisfy a
    real disk's recorded identity even if the sizes happen to agree.
    """

    cls: str
    size_bytes: int
    id_model: str
    id_serial: str

    def to_json(self) -> dict:
        """report.json / state.json shape.

        Field names stay sysfs_model / sysfs_serial for continuity with the
        spec's section 12 example; for loop and dm they carry that class's
        substitutes.
        """
        return {
            "class": self.cls,
            "size_bytes": self.size_bytes,
            "sysfs_model": self.id_model,
            "sysfs_serial": self.id_serial,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Identity":
        return cls(
            cls=data["class"],
            size_bytes=int(data["size_bytes"]),
            id_model=data["sysfs_model"],
            id_serial=data["sysfs_serial"],
        )

    def describe_mismatch(self, other: "Identity") -> str:
        parts = []
        for name, mine, theirs in (
            ("class", self.cls, other.cls),
            ("size_bytes", self.size_bytes, other.size_bytes),
            ("model", self.id_model, other.id_model),
            ("serial", self.id_serial, other.id_serial),
        ):
            if mine != theirs:
                parts.append(f"{name}: recorded {mine!r}, found {theirs!r}")
        return "; ".join(parts) if parts else "(no differences)"


def size_bytes_of(sysdir: Path) -> int:
    """Device length from sysfs.

    /sys/block/<kname>/size is ALWAYS in 512-byte units, even on a 4Kn drive.
    Multiplying it by the logical block size is an 8x overrun bug (spec 4.6,
    Appendix A). This is the one place that conversion happens.
    """
    sectors = _read_int(sysdir / "size")
    if sectors is None:
        raise IdentityError(f"{sysdir.name}: cannot read {sysdir / 'size'}")
    return sectors * 512


def _udev_property(kname: str, prop: str) -> str | None:
    try:
        out = subprocess.run(
            ["udevadm", "info", "--query=property", f"--name={kname}"],
            capture_output=True, text=True, timeout=15, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        _log.debug("udevadm failed for %s: %s", kname, exc)
        return None
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key == prop and value.strip():
            return value.strip()
    return None


def _decode_vpd_pg80(sysdir: Path) -> str | None:
    """SCSI VPD page 0x80 is the unit serial number page.

    Layout: byte 0 peripheral qualifier/type, byte 1 page code (0x80),
    bytes 2-3 big-endian page length, then that many bytes of serial.
    """
    try:
        blob = (sysdir / "device" / "vpd_pg80").read_bytes()
    except OSError:
        return None
    if len(blob) < 4 or blob[1] != 0x80:
        return None
    length = int.from_bytes(blob[2:4], "big")
    serial = blob[4:4 + length].decode("ascii", errors="replace").strip()
    return serial or None


def _scsi_identity(sysdir: Path) -> Identity:
    kname = sysdir.name
    model = _read(sysdir / "device" / "model")
    if not model:
        raise IdentityError(f"{kname}: no device/model; cannot establish identity")

    serial = _udev_property(kname, "ID_SERIAL_SHORT") or _decode_vpd_pg80(sysdir)
    if not serial:
        # Not fatal on its own -- some bridges expose neither. Identity then
        # rests on class + size + model, which is weaker but still catches the
        # cases that matter (a different-size or different-model device
        # appearing under the same kernel name). Recorded explicitly rather than
        # silently blanked so the report can show it.
        _log.warning(
            "%s: no ID_SERIAL_SHORT and no readable vpd_pg80; identity will rest "
            "on class, size and model only", kname,
        )
        serial = ""
    return Identity(CLASS_SCSI, size_bytes_of(sysdir), model.strip(), serial.strip())


def _loop_identity(sysdir: Path) -> Identity:
    backing = _read(sysdir / "loop" / "backing_file")
    if not backing:
        raise IdentityError(f"{sysdir.name}: loop device has no backing_file")
    offset = _read(sysdir / "loop" / "offset") or "0"
    return Identity(CLASS_LOOP, size_bytes_of(sysdir), "loop", f"{backing}:{offset}")


def _dm_identity(sysdir: Path) -> Identity:
    serial = _read(sysdir / "dm" / "uuid") or _read(sysdir / "dm" / "name")
    if not serial:
        raise IdentityError(f"{sysdir.name}: dm device has neither uuid nor name")
    return Identity(CLASS_DM, size_bytes_of(sysdir), "dm", serial)


_IDENTITY_BUILDERS = {
    CLASS_SCSI: _scsi_identity,
    CLASS_LOOP: _loop_identity,
    CLASS_DM: _dm_identity,
}


def identity_of(kname: str) -> Identity:
    """Compute the identity tuple for a kernel name, dispatching on class."""
    sysdir = SYS_BLOCK / kname
    if not sysdir.is_dir():
        raise IdentityError(f"{kname}: no such block device in /sys/block")
    return _IDENTITY_BUILDERS[classify(sysdir)](sysdir)


def identity_of_fd(fd: int) -> tuple[Identity, str]:
    """Recompute identity from an OPEN file descriptor (spec 4.5 step 3).

    This is the check that matters. It interrogates the object that is about to
    be written rather than a path that may since have been reassigned, so it
    works identically for by-id drives, --device drives, and test fixtures.

    Returns (identity, resolved_kernel_name).
    """
    st = os.fstat(fd)
    major, minor = os.major(st.st_rdev), os.minor(st.st_rdev)
    devdir = SYS_DEV_BLOCK / f"{major}:{minor}"
    try:
        kname = os.path.basename(os.path.realpath(devdir))
    except OSError as exc:
        raise IdentityError(f"cannot resolve {devdir} from open fd: {exc}") from exc
    if not (SYS_BLOCK / kname).is_dir():
        raise IdentityError(
            f"fd resolves to {major}:{minor} -> {kname!r}, which is not a whole "
            f"disk in /sys/block. Refusing to continue."
        )
    return identity_of(kname), kname


def assert_matches(recorded: Identity, fd: int) -> str:
    """Verify an open fd is the device we recorded. Returns the kernel name.

    Raises IdentityError on any mismatch. Callers must treat this as fatal for
    the drive; there is no retry that makes a mismatched identity safe.
    """
    found, kname = identity_of_fd(fd)
    if found != recorded:
        raise IdentityError(
            "identity mismatch on open descriptor -- refusing to touch this "
            f"device. {recorded.describe_mismatch(found)}. Resolved kernel name "
            f"is {kname!r}."
        )
    return kname


# --------------------------------------------------------------------------
# Locators (spec 4.5, 6.4)
# --------------------------------------------------------------------------

_HCTL_RE = re.compile(r"/(\d+:\d+:\d+:\d+)$")
# USB port paths look like 2-1.4 or 2-1.4:1.0; we want the bus-port form.
_USB_PORT_RE = re.compile(r"/(\d+-[\d.]+)(?::[\d.]+)?/")


@dataclass
class LocatorEpoch:
    """One (locator, time window) pair (spec 6.4).

    A disconnect and reconnect usually changes the kernel name and often the
    H:C:T:L, so a locator captured once at first open stops matching partway
    through and the tool silently under-reports exactly the I/O errors the
    kernel log sweep exists to surface. Hence a list of epochs, each bounded to
    its own window -- that bounding is what keeps a reused kernel name from
    pulling in another drive's messages.
    """

    hctl: str | None
    usb_port_path: str | None
    kernel_name: str
    valid_from: str
    valid_until: str | None = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "LocatorEpoch":
        return cls(**data)

    def close(self, when: str | None = None) -> None:
        if self.valid_until is None:
            self.valid_until = when or log.utcstamp()


def locator_of(kname: str) -> tuple[str | None, str | None]:
    """Read the locator fields for a kernel name.

    Returns (hctl, usb_port_path). Both are None for loop and dm devices, which
    have no device symlink to resolve and are excluded from kernel-log
    correlation entirely.
    """
    sysdir = SYS_BLOCK / kname
    devlink = sysdir / "device"
    if not devlink.exists():
        return None, None
    try:
        target = os.path.realpath(devlink)
    except OSError:
        return None, None

    hctl = None
    if (m := _HCTL_RE.search(target)):
        hctl = m.group(1)

    usb_port = None
    if (m := _USB_PORT_RE.search(target + "/")):
        usb_port = m.group(1)

    return hctl, usb_port


@dataclass
class LocatorHistory:
    """The append-only epoch list carried in the checkpoint."""

    epochs: list[LocatorEpoch] = field(default_factory=list)

    def open_epoch(self, kname: str, when: str | None = None) -> LocatorEpoch:
        """Close any open epoch and start a new one for the current locator."""
        when = when or log.utcstamp()
        self.close_current(when)
        hctl, port = locator_of(kname)
        epoch = LocatorEpoch(hctl=hctl, usb_port_path=port,
                             kernel_name=kname, valid_from=when)
        self.epochs.append(epoch)
        _log.debug("locator epoch opened: %s", epoch)
        return epoch

    def close_current(self, when: str | None = None) -> None:
        if self.epochs:
            self.epochs[-1].close(when)

    @property
    def current(self) -> LocatorEpoch | None:
        return self.epochs[-1] if self.epochs else None

    def to_json(self) -> list[dict]:
        return [e.to_json() for e in self.epochs]

    @classmethod
    def from_json(cls, data: list[dict] | None) -> "LocatorHistory":
        return cls(epochs=[LocatorEpoch.from_json(d) for d in (data or [])])
