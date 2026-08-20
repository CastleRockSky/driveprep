"""Logging for DrivePrep.

Two sinks: the console (what the operator watches) and a per-drive run.log (the
archived record that ships alongside the report). Both carry ISO-8601 UTC
timestamps so lines can be correlated against the kernel log sweep in kernlog.py,
which is the whole reason the format is pinned rather than left to logging's
defaults.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()
_CONFIGURED = False

ISO = "%Y-%m-%dT%H:%M:%S"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcstamp(dt: datetime | None = None) -> str:
    """RFC3339 UTC with a Z suffix, as used everywhere in report.json."""
    dt = dt or utcnow()
    return dt.astimezone(timezone.utc).strftime(ISO) + "Z"


class _UTCFormatter(logging.Formatter):
    converter = staticmethod(lambda ts: datetime.fromtimestamp(ts, timezone.utc).timetuple())

    def formatTime(self, record, datefmt=None):  # noqa: N802 - logging API
        dt = datetime.fromtimestamp(record.created, timezone.utc)
        return dt.strftime(ISO) + "Z"


_CONSOLE_FMT = _UTCFormatter("%(asctime)s %(levelname)-7s %(drive)s%(message)s")
_FILE_FMT = _UTCFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")


class _DriveFilter(logging.Filter):
    """Prefixes console lines with the drive id when one is bound.

    In queue mode four children interleave on one terminal, so an unlabelled
    line is close to useless.
    """

    def __init__(self, drive_id: str | None = None):
        super().__init__()
        self.drive_id = drive_id

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "drive"):
            record.drive = f"[{self.drive_id}] " if self.drive_id else ""
        return True


def setup(verbose: bool = False, drive_id: str | None = None) -> None:
    """Configure the root logger for console output. Idempotent."""
    global _CONFIGURED
    with _LOCK:
        if _CONFIGURED:
            return
        root = logging.getLogger("driveprep")
        root.setLevel(logging.DEBUG if verbose else logging.INFO)
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_CONSOLE_FMT)
        handler.addFilter(_DriveFilter(drive_id))
        root.addHandler(handler)
        root.propagate = False
        _CONFIGURED = True


def add_run_log(path: Path) -> logging.Handler:
    """Attach a per-drive run.log handler. Returns it so it can be removed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(_FILE_FMT)
    handler.setLevel(logging.DEBUG)
    logging.getLogger("driveprep").addHandler(handler)
    return handler


def remove_handler(handler: logging.Handler) -> None:
    logging.getLogger("driveprep").removeHandler(handler)
    handler.close()


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"driveprep.{name}")


def require_root() -> None:
    """Spec section 2: fail fast, with a message that says what to do."""
    if os.geteuid() != 0:
        # argv[0] is a module path under `python -m`; show the command the
        # operator actually types.
        invocation = " ".join(["driveprep", *sys.argv[1:]])
        sys.stderr.write(
            "driveprep must run as root: it opens block devices directly and reads\n"
            "the kernel log. Re-run with sudo:\n\n"
            f"    sudo {invocation}\n\n"
        )
        raise SystemExit(2)
