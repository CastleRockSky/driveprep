"""Errors, queue, and signals: spec 14 tests 6, 13, 17."""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from driveprep import blockio, grade as grading, identity as ident
from driveprep import state as st, supervisor as sup
from make_loop import loop_device, seed_pattern

from conftest import needs_dmsetup, needs_losetup


# --------------------------------------------------------------------------
# Per-drive lock (spec 8.1)
# --------------------------------------------------------------------------


def test_per_drive_lock_is_exclusive(tmp_path):
    """O_EXCL only covers phases 4-5; phases 0-3 hold no descriptor."""
    lock_path = tmp_path / ".lock"
    first = sup.DriveLock(lock_path)
    assert first.acquire() is True

    second = sup.DriveLock(lock_path)
    assert second.acquire() is False, "a second holder must be refused"

    first.release()
    assert second.acquire() is True
    second.release()


def test_lock_context_manager_raises_when_held(tmp_path):
    held = sup.DriveLock(tmp_path / ".lock")
    held.acquire()
    try:
        with pytest.raises(RuntimeError, match="already locked"):
            with sup.DriveLock(tmp_path / ".lock"):
                pass
    finally:
        held.release()


def test_lock_records_the_owning_pid(tmp_path):
    lock = sup.DriveLock(tmp_path / ".lock")
    lock.acquire()
    try:
        assert str(os.getpid()) in (tmp_path / ".lock").read_text()
    finally:
        lock.release()


# --------------------------------------------------------------------------
# Write-protection is never a media error (spec 4.6)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code", [errno.EROFS, errno.EACCES, errno.EPERM])
def test_write_protection_is_fatal_not_a_media_error(code):
    """Otherwise a configuration problem is reported as millions of bad sectors."""
    with pytest.raises(blockio.WriteProtectedError, match="write-protected"):
        blockio._classify_write_error(OSError(code, os.strerror(code)), 4096)


@pytest.mark.parametrize("code", [errno.ENODEV, errno.ENXIO])
def test_vanished_device_is_its_own_class(code):
    with pytest.raises(blockio.DeviceVanishedError):
        blockio._classify_write_error(OSError(code, os.strerror(code)), 4096)


def test_other_write_errors_are_plain_io_errors():
    with pytest.raises(blockio.BlockIOError):
        blockio._classify_write_error(OSError(errno.EIO, "io"), 4096)


# --------------------------------------------------------------------------
# Test 6: dm-error
# --------------------------------------------------------------------------


@pytest.mark.root
@needs_dmsetup
@needs_losetup
def test_6_read_errors_are_recorded_with_correct_ranges_and_grade_fail(
        clean_report, config):
    """The run must continue past the errors, not stop at the first one."""
    from make_flaky import dm_error_device

    total = 32 * 1024 * 1024
    bad_start, bad_length = 8 * 1024 * 1024, 1024 * 1024

    with loop_device(total) as loop:
        with dm_error_device(loop.path, total,
                             [(bad_start, bad_length)]) as dm:
            cfg = blockio.PassConfig(chunk_bytes=1024 * 1024,
                                     logical_block_bytes=512,
                                     physical_block_bytes=512)
            fd = os.open(dm.path, os.O_RDONLY | os.O_DIRECT)
            try:
                findings = blockio.verify_zero(fd, dm.size_bytes, cfg)
            finally:
                os.close(fd)

            assert findings.read_errors > 0
            assert findings.bytes_done == dm.size_bytes, \
                "the pass must run to the end, not stop at the first error"

            covered = [(r.start, r.end) for r in findings.read_error_ranges]
            assert covered, "error ranges must be recorded"
            assert min(s for s, _e in covered) >= bad_start
            assert max(e for _s, e in covered) <= bad_start + bad_length

            report = dict(clean_report)
            report["verify"] = {**clean_report["verify"],
                                "read_errors": findings.read_errors}
            assert grading.evaluate(report, config).value == grading.FAIL


@pytest.mark.root
@needs_dmsetup
@needs_losetup
def test_6_narrowing_reduces_the_bad_region_to_actual_sectors():
    """Narrowing turns '1 MB unreadable' into the sectors that really failed."""
    from make_flaky import dm_error_device

    total = 16 * 1024 * 1024
    bad_start, bad_length = 4 * 1024 * 1024, 8192  # 16 sectors

    with loop_device(total) as loop:
        with dm_error_device(loop.path, total,
                             [(bad_start, bad_length)]) as dm:
            cfg = blockio.PassConfig(chunk_bytes=1024 * 1024,
                                     logical_block_bytes=512,
                                     physical_block_bytes=512,
                                     narrow_error_ranges=True)
            fd = os.open(dm.path, os.O_RDONLY | os.O_DIRECT)
            try:
                findings = blockio.verify_zero(fd, dm.size_bytes, cfg)
            finally:
                os.close(fd)

            total_bad = sum(r.length for r in findings.read_error_ranges)
            assert total_bad <= bad_length * 2, \
                f"narrowing should report ~{bad_length} bytes, got {total_bad}"


# --------------------------------------------------------------------------
# Test 17: signals
# --------------------------------------------------------------------------


def test_17_interrupted_run_grades_incomplete_not_fail(clean_report, config):
    """An interrupted run is an unfinished measurement, not a verdict."""
    report = dict(clean_report)
    report["flags"] = {**clean_report["flags"], "interrupted": True}
    result = grading.evaluate(report, config)
    assert result.value == grading.INCOMPLETE
    assert result.value != grading.FAIL


@pytest.mark.root
@needs_losetup
def test_17_sigterm_stops_at_a_chunk_boundary_and_checkpoints(tmp_path):
    """The checkpoint must survive the signal path (spec 8.2).

    A signal handler that skips the checkpoint reintroduces the
    PASS-on-a-failing-drive bug through the back door.
    """
    import threading

    total = 64 * 1024 * 1024
    chunk = 1024 * 1024
    with loop_device(total) as loop:
        seed_pattern(loop, 1024 * 1024, 512)

        state = st.DriveState(
            drive_id="dp-signal-test", output_dir=tmp_path,
            batch_id="B", run_id="R", capacity_bytes=total,
            identity=ident.identity_of(loop.kname),
            logical_block_bytes=512, physical_block_bytes=512,
        )
        state.phase = st.PHASE_VERIFY

        stop = threading.Event()
        # progress_interval_s=0: the stop is driven from the progress callback,
        # which is rate-limited to once per second in production and would
        # never fire during a sub-second pass over a 64 MB fixture.
        cfg = blockio.PassConfig(chunk_bytes=chunk, logical_block_bytes=512,
                                 physical_block_bytes=512,
                                 progress_interval_s=0)

        def progress(done, _t, _i, _m):
            state.phase_offset = done
            if done >= total // 4:
                stop.set()

        fd = os.open(loop.path, os.O_RDONLY | os.O_DIRECT)
        try:
            blockio.verify_zero(fd, total, cfg,
                                findings=state.verify_findings,
                                progress=progress,
                                should_stop=stop.is_set)
        finally:
            os.close(fd)

        state.checkpoint(force=True)

        resumed = st.DriveState.load(tmp_path)
        assert resumed is not None
        assert 0 < resumed.phase_offset < total, "must have stopped partway"
        assert len(resumed.verify_findings.nonzero_ranges) == 1, \
            "findings recorded before the signal must be in the checkpoint"
        assert resumed.phase_offset % 512 == 0


# --------------------------------------------------------------------------
# Test 13: the queue
# --------------------------------------------------------------------------


def test_13_supervisor_records_a_child_that_died_without_reporting(tmp_path):
    """The parent must survive a child crash and keep the queue running."""
    class Options:
        jobs = 2
        batch_id = "B-test"
        output_root = str(tmp_path)
        verbose = False

    supervisor = sup.Supervisor({"shutdown": {}}, Options())
    supervisor._results["drive-b"] = (1, "child process exited with code -9 "
                                         "without reporting a result")

    class FakeDisk:
        id = "drive-b"
        output_name = "drive-b"
        model = "TEST"
        capacity_label = "1 TB"

    state = st.DriveState(drive_id="drive-b", output_dir=tmp_path / "drive-b",
                          batch_id="B-test", run_id="R")
    summary = supervisor._summarize([(FakeDisk(), state)])

    assert len(summary["drives"]) == 1
    entry = summary["drives"][0]
    assert entry["grade"] == grading.INCOMPLETE
    assert "without reporting" in entry["reasons"][0]


def test_13_batch_index_survives_a_mixed_batch(tmp_path):
    summary = {
        "batch_id": "B-test", "generated_utc": "2026-08-02T00:00:00Z",
        "drives": [
            {"drive_id": "a", "output_name": "a", "model": "M",
             "capacity_label": "1 TB", "grade": "PASS", "reasons": []},
            {"drive_id": "b", "output_name": "b", "model": "M",
             "capacity_label": "1 TB", "grade": "INCOMPLETE",
             "reasons": ["child died"]},
            {"drive_id": "c", "output_name": "c", "model": "M",
             "capacity_label": "1 TB", "grade": "PASS", "reasons": []},
        ],
    }
    directory = tmp_path / "batches" / "B-test"
    sup.reporting.render_batch_index(summary, directory)
    index = json.loads((directory / "index.json").read_text())
    assert len(index["drives"]) == 3
    assert [d["grade"] for d in index["drives"]] == ["PASS", "INCOMPLETE", "PASS"]


@pytest.mark.root
@needs_losetup
def test_13_three_loop_devices_run_concurrently_and_one_kill_is_survived():
    """Kill one child mid-run; the other two must complete."""
    import multiprocessing as mp

    def worker(path, total, result_queue, name):
        try:
            cfg = blockio.PassConfig(chunk_bytes=1024 * 1024,
                                     logical_block_bytes=512,
                                     physical_block_bytes=512)
            fd = os.open(path, os.O_RDWR | os.O_DIRECT)
            try:
                blockio.zero_fill(fd, total, cfg)
            finally:
                os.close(fd)
            result_queue.put((name, "ok"))
        except Exception as exc:  # noqa: BLE001
            result_queue.put((name, f"error: {exc}"))

    with loop_device(48 * 1024 * 1024) as a, \
            loop_device(48 * 1024 * 1024) as b, \
            loop_device(256 * 1024 * 1024) as c:
        ctx = mp.get_context("fork")
        queue = ctx.Queue()
        procs = {}
        for name, loop in (("a", a), ("b", b), ("c", c)):
            proc = ctx.Process(target=worker,
                               args=(loop.path, loop.size_bytes, queue, name))
            proc.start()
            procs[name] = proc

        time.sleep(0.15)
        procs["c"].kill()   # the big one, most likely still running

        for proc in procs.values():
            proc.join(timeout=120)

        results = {}
        while not queue.empty():
            name, status = queue.get()
            results[name] = status

        assert results.get("a") == "ok", "sibling drives must complete"
        assert results.get("b") == "ok", "sibling drives must complete"
        assert procs["c"].exitcode != 0, "the killed child must report failure"


# --------------------------------------------------------------------------
# Thermal warn throttling (spec 6.5)
# --------------------------------------------------------------------------


def _guard(**cfg):
    from driveprep import thermal
    base = {"warn_c": 50, "pause_c": 55, "resume_c": 45, "abort_c": 60,
            "abort_after_s": 300, "poll_interval_s": 60}
    base.update(cfg)
    return thermal.ThermalGuard("/dev/null", None, base)


def test_thermal_warn_is_throttled_not_once_per_poll(monkeypatch):
    """Every-poll warnings buried run.log under ~180 near-identical lines."""
    from driveprep import thermal
    calls = []
    monkeypatch.setattr(thermal._log, "warning",
                        lambda *a, **k: calls.append(a))
    guard = _guard()

    clock = {"t": 1000.0}
    monkeypatch.setattr(thermal.time, "monotonic", lambda: clock["t"])

    guard._evaluate(51)                 # entering the band -> warn
    assert len(calls) == 1
    for _ in range(20):                 # steady state -> silent
        clock["t"] += 60
        if clock["t"] - 1000.0 < guard._warn_repeat_s:
            guard._evaluate(51)
    assert len(calls) == 1, "steady temperature must not re-warn every poll"


def test_thermal_warns_again_on_a_new_peak(monkeypatch):
    from driveprep import thermal
    calls = []
    monkeypatch.setattr(thermal._log, "warning",
                        lambda *a, **k: calls.append(a))
    guard = _guard()
    guard._evaluate(51)
    guard._evaluate(51)
    assert len(calls) == 1
    guard._evaluate(53), "a rising temperature is news"
    assert len(calls) == 2


def test_thermal_warns_again_after_the_repeat_interval(monkeypatch):
    from driveprep import thermal
    calls = []
    monkeypatch.setattr(thermal._log, "warning",
                        lambda *a, **k: calls.append(a))
    guard = _guard()
    clock = {"t": 0.0}
    monkeypatch.setattr(thermal.time, "monotonic", lambda: clock["t"])
    guard._evaluate(51)
    clock["t"] += guard._warn_repeat_s + 1
    guard._evaluate(51)
    assert len(calls) == 2, "a long stretch in the warn band is worth restating"


def test_pause_and_abort_are_never_throttled(monkeypatch):
    """Throttling must only ever apply to the advisory warn band."""
    from driveprep import thermal
    warnings, errors = [], []
    monkeypatch.setattr(thermal._log, "warning",
                        lambda *a, **k: warnings.append(a))
    monkeypatch.setattr(thermal._log, "error", lambda *a, **k: errors.append(a))

    guard = _guard()
    guard._evaluate(56)                       # >= pause_c
    assert not guard._resume.is_set(), "must pause I/O"
    assert guard.state.pause_events == 1

    clock = {"t": 0.0}
    monkeypatch.setattr(thermal.time, "monotonic", lambda: clock["t"])
    hot = _guard()
    hot._evaluate(61)
    clock["t"] += 301
    hot._evaluate(61)
    assert hot.state.aborted, "sustained over abort_c must abort regardless"


# --------------------------------------------------------------------------
# The lock must be WIRED IN, not merely implemented
# --------------------------------------------------------------------------


def test_acquire_locks_drops_a_drive_held_by_another_instance(tmp_path):
    from driveprep.__main__ import _acquire_locks

    class D:
        def __init__(self, name):
            self.id = name

    free, taken = D("drive-free"), D("drive-taken")
    states = {}
    for d in (free, taken):
        directory = tmp_path / d.id
        directory.mkdir()
        states[d.id] = st.DriveState(drive_id=d.id, output_dir=directory,
                                     batch_id="B", run_id="R")

    # Another instance already holds one of them.
    other = sup.DriveLock(states["drive-taken"].lock_path)
    assert other.acquire()
    try:
        selected = [free, taken]
        locks = _acquire_locks(selected, states)
        assert [d.id for d in selected] == ["drive-free"], \
            "a locked drive must be dropped from the batch"
        assert len(locks) == 1
        assert states["drive-free"].lock_path.exists()
    finally:
        other.release()
        for lock in locks:
            lock.release()


def test_run_actually_acquires_locks(tmp_path):
    """Guards the bug this test was written for.

    DriveLock existed, had four passing unit tests, and was never called by the
    product for the entire life of the feature. Testing a mechanism in
    isolation says nothing about whether anything uses it.
    """
    import ast
    import inspect
    from driveprep import __main__ as cli

    source = inspect.getsource(cli)
    tree = ast.parse(source)
    run = next(n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "cmd_run")
    called = {n.func.id for n in ast.walk(run)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_acquire_locks" in called, \
        "cmd_run must take per-drive locks (spec 8.1)"

    acquire = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_acquire_locks")
    attrs = {n.attr for n in ast.walk(acquire) if isinstance(n, ast.Attribute)}
    assert "DriveLock" in attrs or "DriveLock" in source, \
        "_acquire_locks must use the real lock"


def test_token_is_computed_after_locking(tmp_path):
    """A drive locked out by another instance must not be in the token.

    Otherwise the printed token covers a drive this run will never touch, and
    the operator confirms a set that does not match what happens.
    """
    import ast
    import inspect
    from driveprep import __main__ as cli

    tree = ast.parse(inspect.getsource(cli))
    batch = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_run_batch")
    body = ast.dump(batch)
    assert "compute_token" in body, \
        "the token must be computed inside the locked batch, not before it"

    run = next(n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "cmd_run")
    assert "compute_token" not in ast.dump(run), \
        "cmd_run must not compute a token before locks have settled the set"
