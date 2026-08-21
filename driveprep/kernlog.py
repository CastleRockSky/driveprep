"""Kernel log sweep and classification (spec 6.4).

This is a Linux-specific health signal that is genuinely useful in a listing,
and it is hard to get at any other way.

The filter is anchored on the SCSI H:C:T:L address and the USB port path, NOT
on the by-id name. Kernel messages never contain by-id strings: a
blk_update_request or Buffer I/O error line carries only the kernel name, while
driver-level messages are prefixed with the SCSI address
(`sd 6:0:0:0: [sdc] ...`).

Because a disconnect and reconnect usually changes the kernel name and often
the H:C:T:L, matching is done against a LIST of locator epochs, each bounded to
its own time window. A locator captured once at first open stops matching
partway through, and the tool then silently under-reports exactly the I/O
errors this module exists to surface. The per-epoch time bound is what keeps a
reused kernel name from pulling in another drive's messages.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
import subprocess
from dataclasses import dataclass, field

from . import identity as ident
from . import log

_log = log.get("kernlog")

# Drive faults. These contribute to FAIL (spec 10.2).
IO_ERROR_PATTERNS = [
    re.compile(r"blk_update_request:\s*(critical\s+)?(I/O|medium)\s+error", re.I),
    re.compile(r"Buffer I/O error", re.I),
    re.compile(r"critical medium error", re.I),
    re.compile(r"Unrecovered read error", re.I),
]

MEDIUM_ERROR_PATTERNS = [
    re.compile(r"critical medium error", re.I),
    re.compile(r"Sense Key\s*:\s*Medium Error", re.I),
]

# Usually cable, port, hub, or power problems rather than platter problems.
# These contribute to CAUTION with an explicit note saying so.
USB_RESET_PATTERNS = [
    re.compile(r"reset (high|full|low|super)[- ]?speed(\s+plus)? USB device",
               re.I),
    re.compile(r"usb \S+: USB disconnect", re.I),
    re.compile(r"device descriptor read/\d+, error", re.I),
    re.compile(r"unable to enumerate USB device", re.I),
]

UAS_ABORT_PATTERNS = [
    re.compile(r"uas_eh_abort_handler", re.I),
    re.compile(r"task abort", re.I),
    re.compile(r"Device offlined", re.I),
    re.compile(r"command timed out", re.I),
]

CATEGORIES = ("io_errors", "medium_errors", "usb_resets", "uas_aborts")


@dataclass
class KernelEvents:
    counts: dict = field(default_factory=lambda: {c: 0 for c in CATEGORIES})
    lines: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return dict(self.counts)

    @property
    def has_drive_fault(self) -> bool:
        """I/O or medium errors -- these are the FAIL-contributing category."""
        return self.counts["io_errors"] > 0 or self.counts["medium_errors"] > 0

    @property
    def has_link_fault(self) -> bool:
        """Resets and aborts -- the CAUTION-contributing, cable-class category."""
        return self.counts["usb_resets"] > 0 or self.counts["uas_aborts"] > 0

    def merge(self, other: "KernelEvents") -> None:
        for key in CATEGORIES:
            self.counts[key] += other.counts[key]
        self.lines.extend(other.lines)


def _journal(since: str, until: str | None = None) -> list[str]:
    cmd = ["journalctl", "-k", "--since", since, "--output=short-iso-precise", "--utc", "--no-pager"]
    if until:
        cmd += ["--until", until]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("journalctl unavailable (%s); kernel log evidence will be "
                     "missing from this report", exc)
        return []
    return (proc.stdout or "").splitlines()


def _iso_for_journal(stamp: str) -> str:
    """Render a UTC instant as '@<epoch seconds>' for journalctl.

    Epoch timestamps are RFC3339 UTC ('2026-08-19T18:17:29Z'). The obvious
    conversion -- drop the T and the Z -- produces a bare wall-clock string,
    and journalctl reads bare timestamps in LOCAL time. On any host not set to
    UTC that silently shifts every scan window by the UTC offset: six hours on
    America/Denver, which is how a run with a real USB disconnect inside its
    window reported usb_resets=0.

    '@<seconds>' is unambiguous: systemd always reads it as seconds since the
    Unix epoch, so the query means the same thing in every timezone.
    """
    text = stamp.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # Not a shape we recognise; hand it through untouched rather than
        # inventing a window, and let journalctl decide.
        return text.replace("T", " ").rstrip("Z")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return f"@{int(parsed.timestamp())}"


def _shift_seconds(stamp: str, delta: int) -> str:
    """Move an '@<epoch>' query bound by delta seconds."""
    if stamp.startswith("@"):
        try:
            return f"@{int(stamp[1:]) + delta}"
        except ValueError:
            return stamp
    return stamp


def _line_matches_epoch(line: str, epoch: ident.LocatorEpoch) -> bool:
    """Does this kernel line belong to the drive during this epoch?

    Matches on any of: the SCSI address prefix, the USB port path, or the
    bracketed kernel name that sd prints. All three are epoch-scoped, and the
    caller has already bounded the line to the epoch's time window.
    """
    if epoch.hctl and f" {epoch.hctl}:" in line:
        return True
    if epoch.usb_port_path and re.search(
        rf"usb\s+{re.escape(epoch.usb_port_path)}[:\s]", line
    ):
        return True
    if re.search(rf"\[{re.escape(epoch.kernel_name)}\]", line):
        return True
    # Bare 'sdc:' form used by some drivers.
    if re.search(rf"\b{re.escape(epoch.kernel_name)}:\s", line):
        return True
    # 'dev sdc,' / 'on dev sdc1,' -- the form used by the two most important
    # patterns in this module, blk_update_request and Buffer I/O error. These
    # carry no SCSI prefix and no brackets, so without this clause the drive
    # faults that contribute to FAIL are the ones that get missed.
    # \d* admits partitions of the same disk; the trailing \b keeps 'sdc' from
    # matching a different disk named 'sdca'.
    if re.search(rf"\bdev {re.escape(epoch.kernel_name)}\d*\b", line):
        return True
    return False


def _classify(line: str) -> str | None:
    for pattern in MEDIUM_ERROR_PATTERNS:
        if pattern.search(line):
            return "medium_errors"
    for pattern in IO_ERROR_PATTERNS:
        if pattern.search(line):
            return "io_errors"
    for pattern in USB_RESET_PATTERNS:
        if pattern.search(line):
            return "usb_resets"
    for pattern in UAS_ABORT_PATTERNS:
        if pattern.search(line):
            return "uas_aborts"
    return None


def journal_covers(stamp: str) -> bool:
    """Does the journal still reach back to this instant?

    Distinguishes a genuinely clean run from one whose evidence has been
    rotated away. Both yield an empty sweep, and reporting them the same way
    would either understate a clean result or invent one.
    """
    lines = _journal("@0")
    for line in lines:
        head = line.split(" ", 1)[0]
        try:
            earliest = datetime.fromisoformat(head.replace("Z", "+00:00"))
        except ValueError:
            continue
        try:
            want = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
        except ValueError:
            return False
        if want.tzinfo is None:
            want = want.replace(tzinfo=timezone.utc)
        return earliest <= want
    return False


def sweep(history: ident.LocatorHistory, run_start: str) -> KernelEvents:
    """Collect and classify kernel messages for one drive across all epochs.

    Each epoch is queried over its own time window, which is what stops a
    kernel name that has since been reassigned to another drive from
    contributing that drive's errors to this report.
    """
    events = KernelEvents()
    if not history.epochs:
        return events

    seen: set[str] = set()
    for epoch in history.epochs:
        since = _iso_for_journal(epoch.valid_from or run_start)
        # journalctl's --until is EXCLUSIVE at one-second granularity, and an
        # epoch's valid_until is the instant the locator changed -- normally a
        # USB disconnect. Querying the raw boundary therefore drops the very
        # event that closed the epoch. Widening by one second is what makes a
        # disconnect visible to the report at all.
        until = (_shift_seconds(_iso_for_journal(epoch.valid_until), 1)
                 if epoch.valid_until else None)
        for line in _journal(since, until):
            if not _line_matches_epoch(line, epoch):
                continue
            category = _classify(line)
            if category is None:
                continue
            # Consecutive epochs can abut, and the one-second widening above
            # can then show the same line to both. Count each line once.
            if line in seen:
                continue
            seen.add(line)
            events.counts[category] += 1
            events.lines.append(line)

    if events.lines:
        _log.info(
            "kernel log: %s",
            ", ".join(f"{k}={v}" for k, v in events.counts.items() if v),
        )
    return events


def write_log_file(events: KernelEvents, path) -> None:
    """Matched lines are stored verbatim; the report shows counts only."""
    header = [
        "# DrivePrep kernel log evidence",
        "# Lines matched to this device across all locator epochs (spec 6.4).",
        "# Counts by category: "
        + ", ".join(f"{k}={v}" for k, v in events.counts.items()),
        "",
    ]
    path.write_text("\n".join(header + events.lines) + "\n", encoding="utf-8")
