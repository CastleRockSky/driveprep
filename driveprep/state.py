"""Checkpoint, progress, and resume (spec 9.1).

Two files per drive, and keeping them separate is deliberate:

  state.json     the durable checkpoint. Written atomically every 30 seconds
                 and at every phase boundary. Read on resume.

  progress.json  disposable. Rewritten up to once per second purely so the
                 parent can render its status table. Never read on resume;
                 losing or corrupting it costs nothing.

Without that split, a once-per-second atomic rewrite of the full findings
structure would be on the hot path of a 20-hour run.

The accumulated-findings requirement in this module is not optional. Section 10
grades FAIL on any read error or any nonzero sector, so if findings are not
checkpointed and re-hydrated on resume, a run interrupted at 40 percent that
had already hit 30 read errors resumes with a clean slate and grades PASS on a
failing drive. That is the single most dangerous bug available in this design,
because it produces a confident, wrong document that a buyer relies on.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import SCHEMA_VERSION
from . import blockio
from . import identity as ident
from . import log

_log = log.get("state")

PHASE_INVENTORY = 0
PHASE_SMART_BEFORE = 1
PHASE_SHORT_TEST = 2
PHASE_CONFIRM = 3
PHASE_ERASE = 4
PHASE_VERIFY = 5
PHASE_EXTENDED_TEST = 6
PHASE_SMART_AFTER = 7
PHASE_REPORT = 8

PHASE_NAMES = {
    0: "inventory", 1: "smart_before", 2: "short_test", 3: "confirm",
    4: "erase", 5: "verify", 6: "extended_test", 7: "smart_after",
    8: "report",
}


def atomic_write_json(path: Path, payload: dict) -> None:
    """Write to .tmp then os.replace(), so a crash never leaves a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


@dataclass
class DriveState:
    """Everything needed to resume a drive without trusting the kernel name."""

    drive_id: str
    output_dir: Path
    batch_id: str
    run_id: str

    phase: int = PHASE_INVENTORY
    phase_offset: int = 0
    completed_phases: list[int] = field(default_factory=list)

    identity: ident.Identity | None = None
    locators: ident.LocatorHistory = field(default_factory=ident.LocatorHistory)

    by_id: str | None = None
    kernel_name: str | None = None
    model: str = ""
    enclosure_serial: str = ""
    ata_serial: str = ""
    capacity_bytes: int = 0
    logical_block_bytes: int = 512
    physical_block_bytes: int = 4096
    smartctl_d_type: str | None = None

    # Accumulated findings. See the module docstring.
    erase_findings: blockio.Findings = field(default_factory=blockio.Findings)
    verify_findings: blockio.Findings = field(default_factory=blockio.Findings)

    erase_started_utc: str | None = None
    erase_finished_utc: str | None = None
    verify_started_utc: str | None = None
    verify_finished_utc: str | None = None
    run_started_utc: str | None = None

    erase_performed: bool = False
    verify_performed: bool = False

    max_temp_c: int | None = None
    thermal_pause_s: float = 0.0
    thermally_aborted: bool = False
    disconnects: int = 0
    kernel_events: dict = field(default_factory=dict)

    # The parsed "before" SMART snapshot. Persisted rather than held only in
    # memory, because phase 1 runs in the parent before the confirmation gate
    # while the delta is computed in the child -- and because a resumed run
    # must not lose it either, or before_after_delta silently empties.
    smart_before_data: dict | None = None

    short_test: dict | None = None
    extended_test: dict | None = None
    skipped_extended_test: bool = False
    stopped_on_fail: bool = False
    smart_available: bool = True

    incomplete_reason: str | None = None
    failed_reason: str | None = None

    _last_checkpoint: float = field(default=0.0, repr=False)
    _last_progress: float = field(default=0.0, repr=False)

    # -- paths -------------------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.output_dir / "state.json"

    @property
    def progress_path(self) -> Path:
        return self.output_dir / "progress.json"

    @property
    def lock_path(self) -> Path:
        return self.output_dir / ".lock"

    # -- serialisation -----------------------------------------------------

    def to_json(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "drive_id": self.drive_id,
            "batch_id": self.batch_id,
            "run_id": self.run_id,
            "phase": self.phase,
            "phase_name": PHASE_NAMES.get(self.phase, "unknown"),
            "phase_offset": self.phase_offset,
            "completed_phases": self.completed_phases,
            "identity": self.identity.to_json() if self.identity else None,
            "locator_epochs": self.locators.to_json(),
            "by_id": self.by_id,
            "kernel_name": self.kernel_name,
            "model": self.model,
            "enclosure_serial": self.enclosure_serial,
            "ata_serial": self.ata_serial,
            "capacity_bytes": self.capacity_bytes,
            "logical_block_bytes": self.logical_block_bytes,
            "physical_block_bytes": self.physical_block_bytes,
            "smartctl_d_type": self.smartctl_d_type,
            "erase_findings": self.erase_findings.to_json(),
            "verify_findings": self.verify_findings.to_json(),
            "erase_started_utc": self.erase_started_utc,
            "erase_finished_utc": self.erase_finished_utc,
            "verify_started_utc": self.verify_started_utc,
            "verify_finished_utc": self.verify_finished_utc,
            "run_started_utc": self.run_started_utc,
            "erase_performed": self.erase_performed,
            "verify_performed": self.verify_performed,
            "max_temp_c": self.max_temp_c,
            "thermal_pause_s": self.thermal_pause_s,
            "thermally_aborted": self.thermally_aborted,
            "disconnects": self.disconnects,
            "kernel_events": self.kernel_events,
            "smart_before_data": self.smart_before_data,
            "short_test": self.short_test,
            "extended_test": self.extended_test,
            "skipped_extended_test": self.skipped_extended_test,
            "stopped_on_fail": self.stopped_on_fail,
            "smart_available": self.smart_available,
            "incomplete_reason": self.incomplete_reason,
            "failed_reason": self.failed_reason,
        }

    @classmethod
    def load(cls, output_dir: Path) -> "DriveState | None":
        path = output_dir / "state.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log.error("cannot read checkpoint %s: %s", path, exc)
            return None

        logical = data.get("logical_block_bytes", 512)
        state = cls(
            drive_id=data["drive_id"],
            output_dir=output_dir,
            batch_id=data.get("batch_id", ""),
            run_id=data.get("run_id", ""),
            phase=data.get("phase", PHASE_INVENTORY),
            phase_offset=data.get("phase_offset", 0),
            completed_phases=data.get("completed_phases", []),
            identity=(ident.Identity.from_json(data["identity"])
                      if data.get("identity") else None),
            locators=ident.LocatorHistory.from_json(data.get("locator_epochs")),
            by_id=data.get("by_id"),
            kernel_name=data.get("kernel_name"),
            model=data.get("model", ""),
            enclosure_serial=data.get("enclosure_serial", ""),
            ata_serial=data.get("ata_serial", ""),
            capacity_bytes=data.get("capacity_bytes", 0),
            logical_block_bytes=logical,
            physical_block_bytes=data.get("physical_block_bytes", 4096),
            smartctl_d_type=data.get("smartctl_d_type"),
            erase_findings=blockio.Findings.from_json(
                data.get("erase_findings"), logical),
            verify_findings=blockio.Findings.from_json(
                data.get("verify_findings"), logical),
            erase_started_utc=data.get("erase_started_utc"),
            erase_finished_utc=data.get("erase_finished_utc"),
            verify_started_utc=data.get("verify_started_utc"),
            verify_finished_utc=data.get("verify_finished_utc"),
            run_started_utc=data.get("run_started_utc"),
            erase_performed=data.get("erase_performed", False),
            verify_performed=data.get("verify_performed", False),
            max_temp_c=data.get("max_temp_c"),
            thermal_pause_s=data.get("thermal_pause_s", 0.0),
            thermally_aborted=data.get("thermally_aborted", False),
            disconnects=data.get("disconnects", 0),
            kernel_events=data.get("kernel_events", {}),
            smart_before_data=data.get("smart_before_data"),
            short_test=data.get("short_test"),
            extended_test=data.get("extended_test"),
            skipped_extended_test=data.get("skipped_extended_test", False),
            stopped_on_fail=data.get("stopped_on_fail", False),
            smart_available=data.get("smart_available", True),
            incomplete_reason=data.get("incomplete_reason"),
            failed_reason=data.get("failed_reason"),
        )
        _log.info(
            "resumed checkpoint for %s: phase %s at offset %d, %d read error(s) "
            "and %d nonzero range(s) already recorded",
            state.drive_id, PHASE_NAMES.get(state.phase), state.phase_offset,
            state.verify_findings.read_errors,
            len(state.verify_findings.nonzero_ranges),
        )
        return state

    # -- writing -----------------------------------------------------------

    def checkpoint(self, force: bool = False, interval_s: float = 30.0) -> None:
        now = time.monotonic()
        if not force and now - self._last_checkpoint < interval_s:
            return
        atomic_write_json(self.state_path, self.to_json())
        self._last_checkpoint = now

    def enter_phase(self, phase: int, offset: int = 0) -> None:
        """Phase boundary: record the previous phase and checkpoint immediately."""
        if self.phase not in self.completed_phases and self.phase < phase:
            self.completed_phases.append(self.phase)
        self.phase = phase
        self.phase_offset = offset
        self.checkpoint(force=True)
        _log.info("%s: entering phase %d (%s)", self.drive_id, phase,
                  PHASE_NAMES.get(phase, "?"))

    def write_progress(self, bytes_done: int, total: int,
                       inst_mbs: float, mean_mbs: float,
                       interval_s: float = 1.0) -> None:
        now = time.monotonic()
        if now - self._last_progress < interval_s:
            return
        eta = (total - bytes_done) / (mean_mbs * 1e6) if mean_mbs > 0 else None
        payload = {
            "drive_id": self.drive_id,
            "phase": self.phase,
            "phase_name": PHASE_NAMES.get(self.phase, "?"),
            "bytes_done": bytes_done,
            "total_bytes": total,
            "percent": round(100 * bytes_done / total, 2) if total else 0.0,
            "instantaneous_mbs": round(inst_mbs, 1),
            "mean_mbs": round(mean_mbs, 1),
            "eta_seconds": round(eta) if eta else None,
            "updated_utc": log.utcstamp(),
        }
        try:
            # Not atomic and deliberately so: this file is disposable, and a
            # torn read by the parent costs one refresh of a status table.
            self.progress_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass
        self._last_progress = now

    def resume_offset(self, chunk_bytes: int) -> int:
        """Restart offset for an interrupted phase, rounded DOWN to a chunk."""
        return (self.phase_offset // chunk_bytes) * chunk_bytes


def read_progress(output_dir: Path) -> dict | None:
    try:
        return json.loads((output_dir / "progress.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def find_resumable(output_root: Path) -> list[Path]:
    """Output directories holding an interrupted run."""
    if not output_root.is_dir():
        return []
    out = []
    for child in sorted(output_root.iterdir()):
        if child.name == "batches" or not child.is_dir():
            continue
        state = DriveState.load(child)
        if state and state.phase < PHASE_REPORT:
            out.append(child)
    return out
