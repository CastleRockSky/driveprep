"""Multi-drive supervisor (spec 8.1, 8.2).

One OS process per drive, supervised by a parent that owns the queue, the
manifest, and the batch index. The I/O is deliberately not threaded: a stuck
drive should cost one process, not the run.

The parent must survive a child crash -- mark that drive failed with the
reason, keep the rest of the queue running, and include the failure in the
batch index.
"""

from __future__ import annotations

import fcntl
import multiprocessing as mp
import os
import signal
import time
from pathlib import Path

from . import grade as grading
from . import inventory as inv
from . import log
from . import pipeline as pipe
from . import report as reporting
from . import state as st

_log = log.get("supervisor")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INTERRUPTED = 3


class DriveLock:
    """Per-drive flock held for the WHOLE pipeline, not just while writing.

    O_EXCL only protects phases 4 and 5; phases 0 through 3 hold no descriptor
    at all, so without this two operators -- or one operator and a forgotten
    systemd-run unit -- can plan the same drive concurrently and race each
    other's state.json.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._fd)
            self._fd = None
            return False
        os.write(self._fd, f"{os.getpid()}\n".encode())
        return True

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"{self.path}: already locked by another instance")
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# --------------------------------------------------------------------------
# Child
# --------------------------------------------------------------------------


def run_drive(disk: inv.Disk, drive_state: st.DriveState, config: dict,
              options, run_phase_2: bool = False) -> int:
    """Run phases 4-8 for one drive. Executed in a child process.

    Phases 0-2 have already run in the parent, before the confirmation gate,
    unless this is a resume (in which case the parent re-ran eligibility and
    identity and this picks up where the checkpoint left off).
    """
    stop = _install_child_signals()
    log.setup(verbose=options.verbose, drive_id=disk.id)
    handler = log.add_run_log(drive_state.output_dir / "run.log")

    pipeline = pipe.DrivePipeline(disk, drive_state, config, options, stop_flag=stop)
    interrupted = False
    too_many_disconnects = False

    try:
        # Phase 2 already ran in the parent, before the confirmation gate
        # (spec 7). run_phase_2 is retained for `resume`, which re-enters
        # mid-pipeline and may not have a fresh short-test result.
        if run_phase_2 and not pipeline.phase2_short_test():
            report = pipeline.build_report()
            pipeline.emit(report)
            return EXIT_FAILED
        if getattr(disk, 'skip_erase', False):
            _log.error('%s: skipped -- it failed the pre-erase health gate',
                       disk.id)
            report = pipeline.build_report()
            pipeline.emit(report)
            return EXIT_FAILED

        if drive_state.phase <= st.PHASE_ERASE:
            pipeline.phase4_erase()
        if drive_state.phase <= st.PHASE_VERIFY:
            pipeline.phase5_verify()
        if drive_state.phase <= st.PHASE_EXTENDED_TEST:
            pipeline.phase6_extended_test()

    except pipe.DriveInterrupted as exc:
        interrupted = True
        drive_state.incomplete_reason = str(exc)
        _log.warning("%s: %s -- checkpointed and stopping", disk.id, exc)
    except pipe.DriveAborted as exc:
        drive_state.incomplete_reason = str(exc)
        too_many_disconnects = "disconnected" in str(exc)
        _log.error("%s: aborted -- %s", disk.id, exc)
    except Exception as exc:  # noqa: BLE001 - a child must never die silently
        drive_state.failed_reason = f"{type(exc).__name__}: {exc}"
        _log.exception("%s: unhandled error", disk.id)

    try:
        pipeline.phase7_smart_after()
    except Exception:  # noqa: BLE001
        _log.exception("%s: could not complete the after-snapshot", disk.id)

    report = pipeline.build_report(interrupted=interrupted,
                                   too_many_disconnects=too_many_disconnects)
    pipeline.emit(report)

    value = report["grade"]["value"]
    if value == grading.INCOMPLETE:
        return EXIT_INTERRUPTED
    return EXIT_OK if value in (grading.PASS, grading.CAUTION) else EXIT_FAILED


def _install_child_signals():
    """Stop at the next chunk boundary and checkpoint (spec 8.2).

    Never bypass the checkpoint here: a signal path that skips it reintroduces
    the PASS-on-a-failing-drive bug through the back door.
    """
    import threading
    stop = threading.Event()

    def handler(signum, _frame):
        _log.warning("received %s; stopping at the next chunk boundary",
                     signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    return stop


def _child_entry(disk, drive_state, config, options, run_phase_2, queue):
    try:
        code = run_drive(disk, drive_state, config, options, run_phase_2)
    except BaseException as exc:  # noqa: BLE001
        queue.put((drive_state.drive_id, EXIT_FAILED, f"{type(exc).__name__}: {exc}"))
        raise
    queue.put((drive_state.drive_id, code, None))
    return code


# --------------------------------------------------------------------------
# Parent
# --------------------------------------------------------------------------


class Supervisor:
    def __init__(self, config: dict, options):
        self.config = config
        self.options = options
        self.jobs = max(1, options.jobs)
        shutdown = config.get("shutdown", {})
        self.child_grace_s = shutdown.get("child_grace_s", 60)
        self.double_interrupt_s = shutdown.get("double_interrupt_s", 5)
        self._stopping = False
        self._last_interrupt = 0.0
        self._children: dict[str, mp.Process] = {}
        self._results: dict[str, tuple[int, str | None]] = {}

    def _install_signals(self) -> None:
        def handler(signum, _frame):
            now = time.monotonic()
            if self._stopping and now - self._last_interrupt < self.double_interrupt_s:
                _log.error("second interrupt -- killing children now")
                for proc in self._children.values():
                    if proc.is_alive():
                        proc.kill()
                raise SystemExit(EXIT_INTERRUPTED)
            self._stopping = True
            self._last_interrupt = now
            _log.warning(
                "received %s: no new drives will start, running drives will "
                "checkpoint and stop. Interrupt again within %d s to kill "
                "immediately.", signal.Signals(signum).name,
                self.double_interrupt_s,
            )
            for proc in self._children.values():
                if proc.is_alive():
                    os.kill(proc.pid, signal.SIGTERM)

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def run(self, work: list[tuple[inv.Disk, st.DriveState, bool]]) -> dict:
        """Run the queue. Returns the batch summary."""
        self._install_signals()
        ctx = mp.get_context("fork")
        queue = ctx.Queue()
        pending = list(work)
        started: list[tuple[inv.Disk, st.DriveState]] = []

        self._warn_topology([d for d, _s, _p in work])

        while pending or self._children:
            while pending and len(self._children) < self.jobs and not self._stopping:
                disk, drive_state, run_phase_2 = pending.pop(0)
                proc = ctx.Process(
                    target=_child_entry,
                    args=(disk, drive_state, self.config, self.options,
                          run_phase_2, queue),
                    name=f"driveprep-{disk.id}",
                )
                proc.start()
                self._children[drive_state.drive_id] = proc
                started.append((disk, drive_state))
                _log.info("started %s (pid %d), %d queued",
                          disk.id, proc.pid, len(pending))

            self._reap(queue)
            self._render_status(started)
            time.sleep(1)

            if self._stopping and not self._children:
                break

        while not queue.empty():
            drive_id, code, error = queue.get()
            self._results[drive_id] = (code, error)

        if self._stopping and pending:
            _log.warning("%d drive(s) never started because the run was "
                         "interrupted", len(pending))

        return self._summarize(started)

    def _reap(self, queue) -> None:
        while not queue.empty():
            drive_id, code, error = queue.get()
            self._results[drive_id] = (code, error)

        for drive_id, proc in list(self._children.items()):
            if proc.is_alive():
                continue
            proc.join(timeout=1)
            del self._children[drive_id]
            if drive_id not in self._results:
                # The child died without reporting -- a crash, an OOM kill, or
                # a SIGKILL. Record it and keep the rest of the queue running.
                self._results[drive_id] = (
                    proc.exitcode or EXIT_FAILED,
                    f"child process exited with code {proc.exitcode} without "
                    f"reporting a result",
                )
                _log.error("%s: child died unexpectedly (exit %s)",
                           drive_id, proc.exitcode)
            else:
                _log.info("%s: finished (exit %s)", drive_id, proc.exitcode)

    def _render_status(self, started) -> None:
        if not self._children:
            return
        rows = []
        for disk, drive_state in started:
            if drive_state.drive_id not in self._children:
                continue
            progress = st.read_progress(drive_state.output_dir)
            if not progress:
                rows.append(f"  {disk.id[:44]:<44} starting")
                continue
            eta = progress.get("eta_seconds")
            eta_text = f"ETA {eta // 3600}h{(eta % 3600) // 60:02d}m" if eta else "ETA --"
            rows.append(
                f"  {disk.id[:44]:<44} {progress['phase_name']:<9} "
                f"{progress['percent']:5.1f}%  {progress['mean_mbs']:6.1f} MB/s  "
                f"{eta_text}"
            )
        if rows:
            _log.debug("status:\n%s", "\n".join(rows))

    def _warn_topology(self, disks: list[inv.Disk]) -> None:
        """USB controller and bus power warnings (spec 8.1)."""
        by_hub: dict[str, list[str]] = {}
        for disk in disks:
            hub = inv.usb_root_hub(disk.kname)
            if hub:
                by_hub.setdefault(hub, []).append(disk.id)
        for hub, members in by_hub.items():
            if len(members) > 4:
                _log.warning(
                    "%d drives share USB host controller %s. Sustained parallel "
                    "writes will contend; consider a lower --jobs or a second "
                    "controller.", len(members), hub,
                )

        portables = [d for d in disks
                     if d.bus_type == inv.BUS_USB
                     and (d.usb_speed_mbps or 0) > 0
                     and d.size_bytes <= 5_000_000_000_000
                     and "passport" in (d.model or "").lower()]
        if len(portables) > 2:
            _log.warning(
                "%d bus-powered 2.5-inch drives in this batch. These brown out "
                "on unpowered hubs under sustained write load, showing up as "
                "USB resets or disconnects. Use a powered hub or fewer "
                "concurrent portables.", len(portables),
            )

    def _summarize(self, started) -> dict:
        drives = []
        for disk, drive_state in started:
            code, error = self._results.get(drive_state.drive_id, (EXIT_FAILED, None))
            report_path = drive_state.output_dir / "report.json"
            value, reasons = grading.INCOMPLETE, []
            if report_path.exists():
                import json
                try:
                    data = json.loads(report_path.read_text(encoding="utf-8"))
                    value = data.get("grade", {}).get("value", grading.INCOMPLETE)
                    reasons = data.get("grade", {}).get("reasons", [])
                except (OSError, json.JSONDecodeError):
                    pass
            if error:
                reasons = [error, *reasons]
            drives.append({
                "drive_id": disk.id,
                "output_name": disk.output_name,
                "model": disk.model,
                "capacity_label": disk.capacity_label,
                "grade": value,
                "reasons": reasons,
                "exit_code": code,
            })
        return {
            "batch_id": self.options.batch_id,
            "generated_utc": log.utcstamp(),
            "drives": drives,
        }


def write_batch_index(summary: dict, output_root: Path) -> Path:
    directory = output_root / "batches" / summary["batch_id"]
    reporting.render_batch_index(summary, directory)
    return directory
