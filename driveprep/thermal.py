"""Thermal guard (spec 6.5).

A stack of drives running full-surface writes in a pile will get hot, and
cooking a drive during the test is a self-inflicted wound.

Runs during phases 4, 5 AND 6. Phase 6 is the extended self-test: 8 to 10 hours
of continuous full-surface activity on a 4 TB drive, which is the longest hot
stretch of the whole pipeline. It cannot be throttled by withholding host I/O
the way phases 4 and 5 can, so there the guard degrades to warn-and-abort.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from . import log
from . import smart

_log = log.get("thermal")


@dataclass
class ThermalState:
    max_temp_c: int | None = None
    paused_seconds: float = 0.0
    pause_events: int = 0
    aborted: bool = False
    abort_reason: str | None = None
    monitoring_possible: bool = True
    samples: list[int] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "temp_max_c": self.max_temp_c,
            "thermal_pause_s": round(self.paused_seconds),
            "thermal_pause_events": self.pause_events,
            "thermally_aborted": self.aborted,
            "thermal_abort_reason": self.abort_reason,
            "thermal_monitoring_possible": self.monitoring_possible,
        }


class ThermalGuard:
    """Polls drive temperature on a background thread and gates I/O.

    The worker calls wait_if_paused() between chunks. That is the only
    interaction point, which keeps the guard out of the hot loop.
    """

    def __init__(
        self,
        dev: str,
        d_type: str | None,
        cfg: dict,
        *,
        phase_allows_pause: bool = True,
        on_abort=None,
    ):
        self.dev = dev
        self.d_type = d_type
        self.warn_c = cfg.get("warn_c", 50)
        self.pause_c = cfg.get("pause_c", 55)
        self.resume_c = cfg.get("resume_c", 45)
        self.abort_c = cfg.get("abort_c", 60)
        self.abort_after_s = cfg.get("abort_after_s", 300)
        self.poll_interval_s = cfg.get("poll_interval_s", 60)
        self.phase_allows_pause = phase_allows_pause
        self.on_abort = on_abort

        self.state = ThermalState()
        self._resume = threading.Event()
        self._resume.set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._paused_since: float | None = None
        self._over_abort_since: float | None = None
        # Warn on entering the band and then only occasionally. Logging every
        # poll put ~180 near-identical lines into run.log over a single 3-hour
        # pass on a warm drive, which buries the events that matter in an
        # artifact that ships with the report.
        self._warned_at: float | None = None
        self._warned_temp: int | None = None
        self._warn_repeat_s = 600

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"thermal-{self.dev}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()
        if self._thread:
            self._thread.join(timeout=self.poll_interval_s + 5)
        self._close_pause()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    # -- worker interface --------------------------------------------------

    def wait_if_paused(self) -> None:
        """Called by the I/O loop between chunks. Blocks while too hot."""
        if not self._resume.is_set():
            self._resume.wait()

    @property
    def aborted(self) -> bool:
        return self.state.aborted

    # -- internals ---------------------------------------------------------

    def _close_pause(self) -> None:
        if self._paused_since is not None:
            self.state.paused_seconds += time.monotonic() - self._paused_since
            self._paused_since = None

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_interval_s):
            try:
                temp = smart.read_temperature(self.dev, self.d_type)
            except smart.SmartError:
                temp = None

            if temp is None:
                if self.state.monitoring_possible:
                    _log.warning(
                        "%s: temperature unavailable; thermal guard is not "
                        "possible for this drive and the report will say so",
                        self.dev,
                    )
                    self.state.monitoring_possible = False
                continue

            self.state.samples.append(temp)
            if self.state.max_temp_c is None or temp > self.state.max_temp_c:
                self.state.max_temp_c = temp

            self._evaluate(temp)

    def _evaluate(self, temp: int) -> None:
        now = time.monotonic()

        if temp >= self.abort_c:
            if self._over_abort_since is None:
                self._over_abort_since = now
            elif now - self._over_abort_since > self.abort_after_s:
                self._abort(
                    f"temperature stayed at or above {self.abort_c} C for more "
                    f"than {self.abort_after_s // 60} minutes despite pausing "
                    f"(peak {self.state.max_temp_c} C). Improve airflow: a box "
                    f"fan pointed at the stack is the actual fix."
                )
                return
        else:
            self._over_abort_since = None

        if not self.phase_allows_pause:
            # Phase 6: a self-test is running on the drive itself and cannot be
            # throttled by withholding host I/O. Abort it instead and let the
            # caller mark the test inconclusive rather than failed.
            if temp >= self.pause_c:
                self._abort(
                    f"reached {temp} C during the extended self-test, which "
                    f"cannot be paused. Self-test aborted; result is "
                    f"inconclusive, not a drive fault."
                )
            elif temp >= self.warn_c:
                _log.warning("%s: %d C during extended self-test", self.dev, temp)
            return

        if not self._resume.is_set():
            if temp <= self.resume_c:
                self._close_pause()
                self._resume.set()
                _log.info("%s: cooled to %d C; resuming I/O", self.dev, temp)
            return

        if temp >= self.pause_c:
            self._resume.clear()
            self._paused_since = now
            self.state.pause_events += 1
            _log.warning(
                "%s: %d C is at or above the %d C pause threshold; pausing I/O "
                "until it drops below %d C", self.dev, temp, self.pause_c,
                self.resume_c,
            )
        elif temp >= self.warn_c:
            self._warn_throttled(temp)
        elif self._warned_at is not None:
            _log.info("%s: back below the %d C warn threshold at %d C",
                      self.dev, self.warn_c, temp)
            self._warned_at = None
            self._warned_temp = None

    def _warn_throttled(self, temp: int) -> None:
        """Log on entering the warn band, on a new peak, then every 10 min."""
        now = time.monotonic()
        first = self._warned_at is None
        hotter = self._warned_temp is not None and temp > self._warned_temp
        stale = self._warned_at is not None and now - self._warned_at >= self._warn_repeat_s

        if first or hotter or stale:
            _log.warning(
                "%s: %d C (warn threshold %d C, pause at %d C)",
                self.dev, temp, self.warn_c, self.pause_c,
            )
            self._warned_at = now
            self._warned_temp = max(temp, self._warned_temp or temp)

    def _abort(self, reason: str) -> None:
        if self.state.aborted:
            return
        self.state.aborted = True
        self.state.abort_reason = reason
        self._resume.set()  # never leave the worker blocked on a dead guard
        _log.error("%s: thermal abort -- %s", self.dev, reason)
        if self.on_abort:
            try:
                self.on_abort(reason)
            except Exception:  # noqa: BLE001 - callback must never mask the abort
                _log.exception("thermal abort callback raised")
