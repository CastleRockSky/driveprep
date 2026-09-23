"""smartctl wrapper: device-type probing, JSON parsing, self-tests (spec 6.1-6.3).

The hard part here is not parsing, it is deciding which -d passthrough type to
trust. A wrong type -- sat,12 above all -- frequently returns structurally valid
but FABRICATED IDENTIFY data rather than an error, so a parseable response is
not an acceptance test. Caching such a type would feed invented values straight
into the grade.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field

from . import log

_log = log.get("smart")

# Probe order for SATA: no -d succeeds on essentially every SATA drive.
SATA_PROBE_TYPES = [None, "sat"]

# Probe order for USB. --scan-open is consulted first by the caller; these are
# the fallbacks in the order the spec lists them.
USB_PROBE_TYPES = ["auto", "sat", "sat,12", "usbjmicron", "usbprolific", "usbsunplus"]

CAPACITY_TOLERANCE = 0.01  # 1%

# How a recorded self-test status bears on the drive (spec 10.2). Only an
# explicit pass passes, and only a status that says nothing about the drive --
# the test did not finish, or its result could not be read -- is excused.
# Everything else fails, so a status nobody anticipated fails rather than
# passes. The previous list of failure needles missed smartctl's real wording
# ("Completed: servo/seek failure", "Completed: electrical failure") and graded
# those drives PASS.
SELFTEST_PASSED = "passed"
SELFTEST_UNFINISHED = "unfinished"
SELFTEST_FAILED = "failed"

_UNFINISHED_SELFTEST_STATUSES = frozenset({
    "inconclusive",                   # ours: the result could not be read back
    "interrupted",                    # ours: the run was stopped
    "unknown", "",                    # no log entry (reports before rubric 2)
    "aborted_by_host",
    "interrupted_(host_reset)",
    "self-test_routine_in_progress",
})


def normalize_selftest_status(text: str | None) -> str:
    """smartctl's status string in the form report.json records it."""
    return (text or "").strip().lower().replace(" ", "_")


def selftest_verdict(status: str | None) -> str:
    status = normalize_selftest_status(status)
    if status == "completed_without_error":
        return SELFTEST_PASSED
    if status in _UNFINISHED_SELFTEST_STATUSES:
        return SELFTEST_UNFINISHED
    return SELFTEST_FAILED


class SmartError(RuntimeError):
    pass


@dataclass
class ProbeAttempt:
    d_type: str | None
    accepted: bool
    reason: str

    def to_json(self) -> dict:
        return {"d_type": self.d_type or "(none)", "accepted": self.accepted,
                "reason": self.reason}


@dataclass
class SmartResult:
    available: bool
    d_type: str | None = None
    data: dict = field(default_factory=dict)
    text: str = ""
    logs: dict = field(default_factory=dict)
    probe_log: list[ProbeAttempt] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "available": self.available,
            "overall_health": self.overall_health,
            "power_on_hours": self.power_on_hours,
            "power_cycles": self.power_cycles,
            "attributes": self.attributes,
            "probe_log": [p.to_json() for p in self.probe_log],
        }

    # -- decoded accessors -------------------------------------------------

    @property
    def overall_health(self) -> str | None:
        status = self.data.get("smart_status")
        if not isinstance(status, dict) or "passed" not in status:
            return None
        return "PASSED" if status["passed"] else "FAILED"

    @property
    def power_on_hours(self) -> int | None:
        """smartctl's DECODED hours, never attribute 9's raw field.

        Attribute 9's raw is vendor-encoded: some firmware reports minutes or
        seconds, and some packs several counters into the 48-bit field. An
        implausible value is treated as unknown so the threshold cannot fire on
        garbage (spec 10.1).
        """
        hours = (self.data.get("power_on_time") or {}).get("hours")
        if hours is None:
            return None
        try:
            hours = int(hours)
        except (TypeError, ValueError):
            return None
        if hours < 0 or hours > 200000:
            _log.warning("implausible power-on hours (%s); recording as unknown", hours)
            return None
        return hours

    @property
    def power_cycles(self) -> int | None:
        value = self.data.get("power_cycle_count")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def temperature_c(self) -> int | None:
        temp = (self.data.get("temperature") or {}).get("current")
        if temp is not None:
            try:
                return int(temp)
            except (TypeError, ValueError):
                pass
        for attr in self.attributes:
            if attr["id"] == 194:
                # Attribute 194's raw often packs min/max into the upper bytes.
                return attr["raw"] & 0xFF
        return None

    @property
    def rotation_rate(self):
        return self.data.get("rotation_rate")

    @property
    def form_factor(self) -> str | None:
        return (self.data.get("form_factor") or {}).get("name")

    @property
    def model(self) -> str | None:
        return self.data.get("model_name") or self.data.get("device_model")

    @property
    def serial(self) -> str | None:
        return self.data.get("serial_number")

    @property
    def firmware(self) -> str | None:
        return self.data.get("firmware_version")

    @property
    def capacity_bytes(self) -> int | None:
        cap = (self.data.get("user_capacity") or {}).get("bytes")
        try:
            return int(cap) if cap is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def attributes(self) -> list[dict]:
        """Flattened attribute table.

        smartctl nests raw as {"value": N, "string": "..."}; report.json carries
        the integer. when_failed stays a STRING ("" / "past" / "now"), never
        null, so spec 10.1's "any non-empty value triggers" rule applies
        directly.
        """
        table = ((self.data.get("ata_smart_attributes") or {}).get("table")) or []
        out = []
        for row in table:
            raw = row.get("raw")
            raw_value = raw.get("value") if isinstance(raw, dict) else raw
            flags = row.get("flags")
            out.append({
                "id": row.get("id"),
                "name": row.get("name"),
                "value": row.get("value"),
                "worst": row.get("worst"),
                "thresh": row.get("thresh"),
                "raw": int(raw_value) if raw_value is not None else 0,
                "flags": (flags or {}).get("string", "") if isinstance(flags, dict) else "",
                "when_failed": row.get("when_failed") or "",
            })
        return out

    def attribute(self, attr_id: int) -> dict | None:
        for attr in self.attributes:
            if attr["id"] == attr_id:
                return attr
        return None

    @property
    def selftest_polling_minutes(self) -> dict:
        poll = ((self.data.get("ata_smart_data") or {}).get("self_test") or {}) \
            .get("polling_minutes") or {}
        return {
            "short": poll.get("short"),
            "extended": poll.get("extended"),
        }


# --------------------------------------------------------------------------
# Running smartctl
# --------------------------------------------------------------------------


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    cmd = ["smartctl", *args]
    _log.debug("running: %s", " ".join(cmd))
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise SmartError(
            "smartctl not found. Install it with: apt install smartmontools"
        ) from exc
    except subprocess.SubprocessError as exc:
        raise SmartError(f"smartctl failed: {exc}") from exc


def _json_probe(dev: str, d_type: str | None) -> dict | None:
    args = ["--json=c", "-i"]
    if d_type:
        args += ["-d", d_type]
    args.append(dev)
    proc = _run(args)
    if not proc.stdout:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def scan_open() -> dict[str, str]:
    """Parse `smartctl --scan-open` once, mapping device path -> -d type.

    This is a GLOBAL scan, not a per-device query. The caller runs it once in
    the parent at inventory time and shares the result with every child rather
    than invoking it once per drive in a parallel batch.
    """
    mapping: dict[str, str] = {}
    try:
        proc = _run(["--scan-open"], timeout=60)
    except SmartError:
        return mapping
    for line in (proc.stdout or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "-d":
            mapping[parts[0]] = parts[2]
    return mapping


def _plausible(data: dict, sysfs_size_bytes: int) -> tuple[bool, str]:
    """Acceptance test for a probe result (spec 6.1).

    Accept only when BOTH hold:
      * capacity agrees with sysfs within one percent -- the load-bearing
        check, reliable on every bus
      * the IDENTIFY response is plausible: non-empty model, firmware and
        serial, and a nonzero capacity

    Deliberately absent: any requirement that the smartctl model match the
    sysfs model. On a USB-bridged drive these are legitimately different --
    sysfs reports the bridge's SCSI INQUIRY model ('Elements 25A2', capped at
    16 characters) while ATA IDENTIFY through the bridge returns the drive's
    own model ('WD40EZRZ-00GXCB0'). A model-equality requirement would reject
    every bridged drive and route them all to the SMART-unavailable path,
    defeating the purpose of the probe.
    """
    if not isinstance(data, dict):
        return False, "no JSON returned"

    cap = (data.get("user_capacity") or {}).get("bytes")
    try:
        cap = int(cap) if cap is not None else 0
    except (TypeError, ValueError):
        cap = 0
    if cap == 0:
        return False, "capacity reported as zero"

    if sysfs_size_bytes:
        drift = abs(cap - sysfs_size_bytes) / sysfs_size_bytes
        if drift > CAPACITY_TOLERANCE:
            return False, (
                f"capacity {cap:,} differs from sysfs {sysfs_size_bytes:,} by "
                f"{drift:.1%} (tolerance {CAPACITY_TOLERANCE:.0%})"
            )

    model = (data.get("model_name") or data.get("device_model") or "").strip()
    firmware = (data.get("firmware_version") or "").strip()
    serial = (data.get("serial_number") or "").strip()
    missing = [n for n, v in (("model", model), ("firmware", firmware),
                              ("serial", serial)) if not v]
    if missing:
        return False, f"IDENTIFY implausible: empty {', '.join(missing)}"

    return True, "capacity within 1% of sysfs and IDENTIFY plausible"


def probe(dev: str, bus_type: str, sysfs_size_bytes: int,
          scan_hint: str | None = None) -> SmartResult:
    """Find a -d type whose IDENTIFY response can be trusted.

    If no candidate passes, SMART is treated as unavailable (spec 6.2) rather
    than falling back to the least-bad guess.
    """
    if bus_type == "ata":
        candidates: list[str | None] = list(SATA_PROBE_TYPES)
    else:
        candidates = []
        if scan_hint:
            candidates.append(scan_hint)
        candidates += [t for t in USB_PROBE_TYPES if t != scan_hint]

    attempts: list[ProbeAttempt] = []
    for d_type in candidates:
        data = _json_probe(dev, d_type)
        if data is None:
            attempts.append(ProbeAttempt(d_type, False, "no parseable JSON"))
            continue
        ok, reason = _plausible(data, sysfs_size_bytes)
        attempts.append(ProbeAttempt(d_type, ok, reason))
        if ok:
            _log.info("%s: SMART via -d %s", dev, d_type or "(none)")
            result = SmartResult(available=True, d_type=d_type, data=data,
                                 probe_log=attempts)
            _enrich(result, dev)
            return result

    _log.warning(
        "%s: no smartctl device type produced trustworthy IDENTIFY data; "
        "treating SMART as unavailable", dev,
    )
    return SmartResult(available=False, probe_log=attempts)


def _enrich(result: SmartResult, dev: str) -> None:
    """Pull the full attribute set and the archived text output."""
    args = ["--json=c", "-x"]
    if result.d_type:
        args += ["-d", result.d_type]
    proc = _run([*args, dev], timeout=180)
    if proc.stdout:
        try:
            result.data.update(json.loads(proc.stdout))
        except json.JSONDecodeError:
            pass

    text_args = ["-x"] + (["-d", result.d_type] if result.d_type else [])
    result.text = _run([*text_args, dev], timeout=180).stdout or ""

    # Raw smartctl -x output is what a skeptical buyer will ask to see, so the
    # log sections are archived verbatim alongside the parsed values.
    for section in ("selftest", "xerror", "devstat", "scterc"):
        largs = ["-l", section] + (["-d", result.d_type] if result.d_type else [])
        result.logs[section] = _run([*largs, dev], timeout=120).stdout or ""


def refresh(dev: str, d_type: str | None) -> SmartResult:
    """Re-read SMART with an already-resolved -d type (for the after snapshot)."""
    data = _json_probe(dev, d_type) or {}
    result = SmartResult(available=bool(data), d_type=d_type, data=data)
    if data:
        _enrich(result, dev)
    return result


def read_temperature(dev: str, d_type: str | None) -> int | None:
    """Cheap temperature poll for the thermal guard (spec 6.5)."""
    args = ["--json=c", "-A"] + (["-d", d_type] if d_type else []) + [dev]
    proc = _run(args, timeout=60)
    if not proc.stdout:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return SmartResult(available=True, d_type=d_type, data=data).temperature_c


# --------------------------------------------------------------------------
# Self-tests (spec 6.3)
# --------------------------------------------------------------------------


@dataclass
class SelfTestResult:
    run: bool
    status: str
    duration_s: int | None = None
    lba_of_first_error: int | None = None

    def to_json(self) -> dict:
        return {
            "run": self.run,
            "status": self.status,
            "duration_s": self.duration_s,
            "lba_of_first_error": self.lba_of_first_error,
        }

    @property
    def fatal(self) -> bool:
        return self.run and selftest_verdict(self.status) == SELFTEST_FAILED


# Poll outcomes from _selftest_state.
_RUNNING = "running"
_IDLE = "idle"            # the drive reports no self-test in progress
_UNREADABLE = "unreadable"


def _selftest_state(dev: str, d_type: str | None) -> tuple[str, int | None]:
    """(_RUNNING / _IDLE / _UNREADABLE, remaining_percent while running).

    UNREADABLE is not "finished". Treating an unparseable answer as finished
    ended the poll early and read back whatever entry topped the log -- often
    the previous test's "Completed without error".
    """
    args = ["--json=c", "-c"] + (["-d", d_type] if d_type else []) + [dev]
    try:
        proc = _run(args, timeout=60)
        data = json.loads(proc.stdout or "{}")
    except (SmartError, json.JSONDecodeError):
        return _UNREADABLE, None
    status = ((data.get("ata_smart_data") or {}).get("self_test") or {}).get("status")
    if not status:
        return _UNREADABLE, None
    remaining = status.get("remaining_percent")
    if remaining is not None:
        return _RUNNING, remaining
    return _IDLE, None


def _selftest_log(dev: str, d_type: str | None) -> dict | None:
    args = ["--json=c", "-l", "selftest"] + (["-d", d_type] if d_type else []) + [dev]
    try:
        proc = _run(args, timeout=60)
        data = json.loads(proc.stdout or "{}")
    except (SmartError, json.JSONDecodeError):
        return None
    return (data.get("ata_smart_self_test_log") or {}).get("standard")


def last_selftest_entry(dev: str, d_type: str | None) -> dict | None:
    table = (_selftest_log(dev, d_type) or {}).get("table")
    return table[0] if table else None


def _log_signature(log_: dict | None):
    """Enough of the self-test log to tell whether a new entry has appeared."""
    if not log_:
        return None
    table = log_.get("table") or []
    return (log_.get("count"),
            json.dumps(table[0], sort_keys=True) if table else None)


def _entry_is_kind(entry: dict, kind: str) -> bool:
    text = ((entry.get("type") or {}).get("string") or "").lower()
    return ("extended" if kind == "long" else kind) in text


# Fastest plausible sustained sequential read for the spinning drives this tool
# supports. Used only to put a FLOOR under a self-test duration, so an
# optimistic figure keeps that floor conservative.
MAX_PLAUSIBLE_READ_MB_S = 250


def extended_test_floor_minutes(
    size_bytes: int | None,
    max_read_mb_s: float = MAX_PLAUSIBLE_READ_MB_S,
) -> int:
    """Lower bound on an extended self-test's duration, from capacity alone.

    An extended test reads every sector, so it cannot finish faster than
    capacity / (fastest plausible sequential read). Even at 250 MB/s -- quicker
    than any drive this tool accepts -- 4 TB takes about 267 minutes.

    Firmware reporting less than that is not describing a real test. A Hitachi
    HUS724040ALE641 reports ONE minute for a 4 TB surface scan, while the HGST
    beside it in the same dock correctly reports 551. Believing the 1 set the
    stall deadline to three minutes, so the run declared the test inconclusive
    at the second poll, ten minutes in -- while the drive carried on scanning
    for another nine hours and ultimately passed. The drive was then graded
    CAUTION for a test it had not failed, and only a manual `recheck` undid it.

    Returns 0 when the capacity is unknown, which leaves the caller's own
    fallback in charge.
    """
    if not size_bytes or size_bytes <= 0 or max_read_mb_s <= 0:
        return 0
    return max(1, int(size_bytes / (max_read_mb_s * 1_000_000) / 60))


def _sleep_unless(should_stop, seconds: float, step: float = 2.0) -> None:
    """Sleep, waking early when a stop is requested.

    One uninterrupted time.sleep(300) meant a Ctrl+C during an extended test
    went unanswered for up to five minutes, long past the parent's grace
    period for a child to checkpoint and stop.
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (should_stop and should_stop()):
            return
        time.sleep(min(step, remaining))


def stall_deadline_s(estimate_s: float, step_factor: float,
                     poll_interval_s: float) -> float:
    """How long remaining_percent may sit still before the test is stalled.

    Drives report progress in 10% steps, so the natural unit is one step --
    a tenth of the estimate -- not the whole test. Measured against the whole
    test, a drive wedged at 90% of a 9-hour scan was waited on for 27 hours
    (observed: 24.7 h, on a drive whose re-run then finished on schedule).
    The floor keeps short tests and long poll intervals from tripping it on
    the first unchanged reading.
    """
    return max(estimate_s / 10 * step_factor, 3 * poll_interval_s, 600)


def run_selftest(
    dev: str,
    d_type: str | None,
    kind: str,
    *,
    poll_interval_s: int,
    estimated_minutes: int | None,
    overrun_warn_factor: float = 1.5,
    no_progress_step_factor: float = 5.0,
    stall_retries: int = 0,
    should_stop=None,
) -> SelfTestResult:
    """Start `smartctl -t <kind>` and poll to completion.

    A test that stalls while the drive still says it is running is wedged:
    it will sit at the same percentage forever and never be logged. Aborting
    it and starting a fresh one has fixed that every time it has been seen,
    so that is retried `stall_retries` times before settling for
    INCONCLUSIVE.
    """
    total = 0
    for attempt in range(stall_retries + 1):
        result, wedged = _run_selftest_once(
            dev, d_type, kind, poll_interval_s=poll_interval_s,
            estimated_minutes=estimated_minutes,
            overrun_warn_factor=overrun_warn_factor,
            no_progress_step_factor=no_progress_step_factor,
            should_stop=should_stop)
        total += result.duration_s or 0
        if not wedged or attempt == stall_retries:
            break
        _log.warning("%s: %s self-test is wedged; aborted it and starting a "
                     "fresh one (retry %d of %d)", dev, kind, attempt + 1,
                     stall_retries)
    if result.run:
        result.duration_s = total
    return result


def _run_selftest_once(
    dev: str,
    d_type: str | None,
    kind: str,
    *,
    poll_interval_s: int,
    estimated_minutes: int | None,
    overrun_warn_factor: float,
    no_progress_step_factor: float,
    should_stop,
) -> tuple[SelfTestResult, bool]:
    """One self-test. Returns (result, wedged).

    Some bridges accept -t but never update the self-test log. If progress
    does not move for stall_deadline_s(), the test is marked INCONCLUSIVE, not
    failed -- an unresponsive bridge is not evidence of a bad drive. It is
    "wedged" only when the drive itself was still reporting the test running,
    which is the case a retry fixes; a bridge that reports nothing will not
    report a second attempt either.

    The result is read from the self-test log only once a NEW entry has
    appeared there. Otherwise the newest entry belongs to an earlier test, and
    reporting it would pass a drive on someone else's result.
    """
    log_before = _log_signature(_selftest_log(dev, d_type))

    args = ["-t", kind] + (["-d", d_type] if d_type else []) + [dev]
    proc = _run(args, timeout=120)
    # smartctl's exit status is a bitmask. Bits 0-1 mean the command line or
    # the device open failed; bit 2 is a non-fatal command grumble. Bits 3-7
    # describe the drive's history -- 64 and 128 are old entries in its error
    # and self-test logs, which most used drives have -- and say nothing about
    # whether the test started. Comparing the whole code recorded those drives
    # as "could not start" while their test ran on regardless.
    if proc.returncode & 0b011:
        return SelfTestResult(run=False, status=f"could_not_start: {proc.stdout.strip()[:120]}"), False

    estimate_s = (estimated_minutes or 5) * 60
    deadline_warn = estimate_s * overrun_warn_factor
    deadline_stall = stall_deadline_s(estimate_s, no_progress_step_factor,
                                      poll_interval_s)

    started = time.monotonic()
    last_remaining = None
    last_change = started
    warned = False
    seen_running = False
    state = None

    while True:
        if should_stop and should_stop():
            # Left alone, the drive carries on testing after the run has
            # stopped, and a later recheck would find a result nobody waited
            # for.
            abort_selftest(dev, d_type)
            return SelfTestResult(run=True, status="interrupted",
                                  duration_s=int(time.monotonic() - started)), False
        _sleep_unless(should_stop, poll_interval_s)
        if should_stop and should_stop():
            continue      # handled at the top of the loop
        elapsed = time.monotonic() - started
        state, remaining = _selftest_state(dev, d_type)

        if state == _RUNNING:
            seen_running = True
        elif _log_signature(_selftest_log(dev, d_type)) != log_before and (
                log_before is not None or (state == _IDLE and seen_running)):
            # A new entry: the test has finished. With no readable log from
            # before the start, "new" cannot be judged, so the drive must also
            # have been seen running and now say it has stopped.
            break
        # Idle or unreadable with no new entry: either the result is not in
        # the log yet or the bridge will never report it. Keep polling and let
        # the stall deadline decide.

        if state == _RUNNING and remaining != last_remaining:
            last_remaining, last_change = remaining, time.monotonic()
        elif time.monotonic() - last_change > deadline_stall:
            wedged = state == _RUNNING
            _log.warning(
                "%s: %s self-test has not advanced in %.0f s%s; marking "
                "inconclusive", dev, kind, time.monotonic() - last_change,
                f" (stuck at {remaining}% remaining)" if wedged else "",
            )
            if wedged:
                # Left running, it holds the drive "in progress" indefinitely.
                abort_selftest(dev, d_type)
            return SelfTestResult(run=True, status="inconclusive",
                                  duration_s=int(elapsed)), wedged

        if not warned and elapsed > deadline_warn:
            _log.warning(
                "%s: %s self-test has run %.0f minutes against an estimate of "
                "%.0f; still waiting", dev, kind, elapsed / 60, estimate_s / 60,
            )
            warned = True

    duration = int(time.monotonic() - started)
    entry = last_selftest_entry(dev, d_type) or {}
    status_obj = entry.get("status") or {}
    if not status_obj.get("string") or not _entry_is_kind(entry, kind):
        _log.warning("%s: newest self-test log entry is not this %s test; "
                     "marking inconclusive", dev, kind)
        return SelfTestResult(run=True, status="inconclusive",
                              duration_s=duration), False
    passed = status_obj.get("passed")

    lba = entry.get("lba")
    # lba_of_first_error comes from smartctl and is passed through UNMODIFIED;
    # it is not recomputed against the logical block size (spec 9).
    try:
        lba = int(lba) if lba is not None else None
    except (TypeError, ValueError):
        lba = None

    status = ("completed_without_error" if passed is True
              else normalize_selftest_status(status_obj["string"]))

    return SelfTestResult(run=True, status=status, duration_s=duration,
                          lba_of_first_error=lba if passed is False else None), False


def abort_selftest(dev: str, d_type: str | None) -> None:
    """smartctl -X. Used on a stop, and by the phase-6 thermal guard (spec 6.5)."""
    args = ["-X"] + (["-d", d_type] if d_type else []) + [dev]
    try:
        _run(args, timeout=60)
    except SmartError as exc:
        _log.warning("could not abort self-test on %s: %s", dev, exc)


def delta(before: SmartResult, after: SmartResult) -> list[dict]:
    """Per-attribute before/after change, for the report."""
    if not (before.available and after.available):
        return []
    before_map = {a["id"]: a for a in before.attributes}
    out = []
    for attr in after.attributes:
        prior = before_map.get(attr["id"])
        if prior is None or prior["raw"] == attr["raw"]:
            continue
        out.append({
            "id": attr["id"],
            "name": attr["name"],
            "before": prior["raw"],
            "after": attr["raw"],
        })
    return out
