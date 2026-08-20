"""Checkpoint and resume: spec 14 tests 4, 5.

Test 5 is the important one. Section 10 grades FAIL on any read error or any
nonzero sector, so if findings are not checkpointed and re-hydrated on resume,
a run interrupted at 40 percent that had already hit 30 read errors resumes
with a clean slate and grades PASS on a failing drive. That produces a
confident, wrong document that a buyer relies on.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from driveprep import blockio, grade as grading, identity as ident, state as st
from make_loop import loop_device, seed_pattern

from conftest import needs_losetup


def _state(tmp_path: Path, **over) -> st.DriveState:
    base = dict(
        drive_id="dp-test", output_dir=tmp_path, batch_id="B-test",
        run_id="R-test", logical_block_bytes=512, physical_block_bytes=512,
        capacity_bytes=64 * 1024 * 1024,
        identity=ident.Identity(ident.CLASS_LOOP, 64 * 1024 * 1024, "loop",
                                "/tmp/x.img:0"),
    )
    base.update(over)
    return st.DriveState(**base)


# --------------------------------------------------------------------------
# Checkpoint mechanics
# --------------------------------------------------------------------------


def test_checkpoint_is_written_atomically(tmp_path):
    state = _state(tmp_path)
    state.checkpoint(force=True)
    assert state.state_path.exists()
    assert not state.state_path.with_suffix(".json.tmp").exists()
    data = json.loads(state.state_path.read_text())
    assert data["drive_id"] == "dp-test"
    assert data["identity"]["class"] == "loop"


def test_progress_is_a_separate_disposable_file(tmp_path):
    """progress.json must never be what resume reads."""
    state = _state(tmp_path)
    state.checkpoint(force=True)
    state.write_progress(1024, 2048, 100.0, 95.0, interval_s=0)
    assert state.progress_path.exists()
    assert state.progress_path != state.state_path

    progress = json.loads(state.progress_path.read_text())
    assert progress["percent"] == 50.0

    # Corrupting it must not affect resume at all.
    state.progress_path.write_text("{ not json")
    assert st.DriveState.load(tmp_path) is not None
    assert st.read_progress(tmp_path) is None


def test_5_findings_survive_a_checkpoint_round_trip(tmp_path):
    """The core of test 5, without needing a block device."""
    state = _state(tmp_path)
    state.phase = st.PHASE_VERIFY
    state.phase_offset = 26 * 1024 * 1024
    for i in range(30):
        state.verify_findings.record_error(
            blockio.Range(i * 8 * 1024 * 1024, 512, 512), cap=1000)
    state.verify_findings.record_nonzero(
        blockio.Range(1024 * 1024, 512, 512), cap=1000)
    state.verify_findings.bytes_done = 26 * 1024 * 1024
    state.checkpoint(force=True)

    resumed = st.DriveState.load(tmp_path)
    assert resumed is not None
    assert resumed.verify_findings.read_errors == 30, \
        "read errors recorded before the interruption must survive resume"
    assert len(resumed.verify_findings.read_error_ranges) == 30
    assert len(resumed.verify_findings.nonzero_ranges) == 1
    assert resumed.phase_offset == 26 * 1024 * 1024


def test_5_a_resumed_run_with_prior_findings_grades_fail(tmp_path, config,
                                                         clean_report):
    """A resumed run that reports clean is the failure this test catches."""
    state = _state(tmp_path)
    state.verify_findings.record_error(blockio.Range(4096, 512, 512), cap=1000)
    state.verify_findings.bytes_done = 26 * 1024 * 1024
    state.checkpoint(force=True)

    resumed = st.DriveState.load(tmp_path)
    report = dict(clean_report)
    report["verify"] = {
        **clean_report["verify"],
        "read_errors": resumed.verify_findings.read_errors,
        "read_error_ranges": [r.to_json()
                              for r in resumed.verify_findings.read_error_ranges],
    }
    assert grading.evaluate(report, config).value == grading.FAIL


def test_resume_offset_rounds_down_to_a_chunk_boundary(tmp_path):
    state = _state(tmp_path)
    state.phase_offset = 26 * 1024 * 1024 + 12345
    assert state.resume_offset(8 * 1024 * 1024) == 24 * 1024 * 1024


def test_phase_transitions_record_completion(tmp_path):
    state = _state(tmp_path)
    state.enter_phase(st.PHASE_ERASE)
    state.enter_phase(st.PHASE_VERIFY)
    assert st.PHASE_ERASE in state.completed_phases
    assert state.phase == st.PHASE_VERIFY


def test_find_resumable_ignores_finished_and_batch_dirs(tmp_path):
    finished = tmp_path / "drive-a"
    interrupted = tmp_path / "drive-b"
    (tmp_path / "batches").mkdir()

    done = _state(finished, drive_id="drive-a")
    done.phase = st.PHASE_REPORT
    done.checkpoint(force=True)

    partial = _state(interrupted, drive_id="drive-b")
    partial.phase = st.PHASE_VERIFY
    partial.checkpoint(force=True)

    resumable = [p.name for p in st.find_resumable(tmp_path)]
    assert resumable == ["drive-b"]


# --------------------------------------------------------------------------
# Tests 4 and 5 on real loop devices
# --------------------------------------------------------------------------


@pytest.mark.root
@needs_losetup
def test_4_interrupted_zero_fill_resumes_at_a_chunk_boundary():
    total = 64 * 1024 * 1024
    chunk = 1024 * 1024
    with loop_device(total) as loop:
        # progress_interval_s=0 is essential here, not cosmetic. Progress is
        # rate-limited to once per second in production -- correct for a
        # 20-hour run -- but a 64 MB loop device finishes in well under a
        # second, so the callback never fires, the stop never triggers, and
        # this test silently passes a complete run off as an interrupted one.
        cfg = blockio.PassConfig(chunk_bytes=chunk, logical_block_bytes=512,
                                 physical_block_bytes=512,
                                 progress_interval_s=0)
        seed_pattern(loop, 0, total)

        stop_at = int(total * 0.4)
        stopped = {"flag": False}

        def should_stop():
            return stopped["flag"]

        def progress(done, _total, _i, _m):
            if done >= stop_at:
                stopped["flag"] = True

        fd = os.open(loop.path, os.O_RDWR | os.O_DIRECT)
        try:
            first = blockio.zero_fill(fd, total, cfg, progress=progress,
                                      should_stop=should_stop)
        finally:
            os.close(fd)

        assert 0 < first.bytes_done < total, "must have stopped partway"
        resume_at = (first.bytes_done // chunk) * chunk

        fd = os.open(loop.path, os.O_RDWR | os.O_DIRECT)
        try:
            blockio.zero_fill(fd, total, cfg, start_offset=resume_at)
        finally:
            os.close(fd)

        fd = os.open(loop.path, os.O_RDONLY | os.O_DIRECT)
        try:
            final = blockio.verify_zero(fd, total, cfg)
        finally:
            os.close(fd)

        assert final.nonzero_ranges == []
        assert final.read_errors == 0


@pytest.mark.root
@needs_losetup
def test_5_findings_from_before_an_interruption_survive_a_real_resume(tmp_path):
    """Seed nonzero data in the first 20%, verify, kill at ~40%, resume.

    The final report must still contain the pre-interruption nonzero ranges and
    grade FAIL.
    """
    total = 64 * 1024 * 1024
    chunk = 1024 * 1024
    with loop_device(total) as loop:
        # See test_4: without progress_interval_s=0 the interruption never
        # happens and this test cannot detect the bug it exists to catch.
        cfg = blockio.PassConfig(chunk_bytes=chunk, logical_block_bytes=512,
                                 physical_block_bytes=512,
                                 progress_interval_s=0)
        seed_pattern(loop, 2 * 1024 * 1024, 512)
        seed_pattern(loop, 6 * 1024 * 1024, 512)

        state = _state(tmp_path, capacity_bytes=total,
                       identity=ident.identity_of(loop.kname))
        stopped = {"flag": False}

        def should_stop():
            return stopped["flag"]

        def progress(done, _t, _i, _m):
            state.phase_offset = done
            if done >= int(total * 0.4):
                stopped["flag"] = True

        fd = os.open(loop.path, os.O_RDONLY | os.O_DIRECT)
        try:
            blockio.verify_zero(fd, total, cfg, findings=state.verify_findings,
                                progress=progress, should_stop=should_stop)
        finally:
            os.close(fd)

        assert len(state.verify_findings.nonzero_ranges) == 2, \
            "both seeded regions are in the first 40% and must be found"
        state.phase = st.PHASE_VERIFY
        state.checkpoint(force=True)

        # --- simulate the process dying and a fresh resume ---
        resumed = st.DriveState.load(tmp_path)
        assert len(resumed.verify_findings.nonzero_ranges) == 2

        fd = os.open(loop.path, os.O_RDONLY | os.O_DIRECT)
        try:
            blockio.verify_zero(
                fd, total, cfg, findings=resumed.verify_findings,
                start_offset=resumed.resume_offset(chunk))
        finally:
            os.close(fd)

        assert len(resumed.verify_findings.nonzero_ranges) == 2, \
            "a resumed run must not report clean on a drive that already failed"
        starts = {r.start for r in resumed.verify_findings.nonzero_ranges}
        assert starts == {2 * 1024 * 1024, 6 * 1024 * 1024}


# --------------------------------------------------------------------------
# Thermal peak must reach the checkpoint DURING a pass (spec 9.1)
# --------------------------------------------------------------------------


def test_max_temperature_is_checkpointed_during_a_pass(tmp_path):
    """Absorbing only at end-of-pass loses the peak on an interrupted run.

    A drive that actually reached 58 C, killed mid-erase and resumed, would
    report a lower peak and could come back PASS instead of CAUTION. Same
    failure shape as findings not surviving a resume.
    """
    from driveprep import pipeline as pipe, thermal

    class Opts:
        chunk_size = None
        test_mode = True
        output_root = str(tmp_path)
        seller_name = ""
        mask_serial = False
        verbose = False
        skip_extended_test = False

    state = _state(tmp_path)
    disk = type("D", (), {"logical_block_bytes": 512,
                          "physical_block_bytes": 512, "id": "dp-test",
                          "size_bytes": 1 << 20})()
    p = pipe.DrivePipeline(disk, state, {"io": {}, "checkpoint": {}}, Opts())

    guard = thermal.ThermalGuard("/dev/null", None, {})
    guard.state.max_temp_c = 58
    p._active_guard = guard

    # A progress tick mid-pass must fold the peak into the checkpoint.
    p._progress(1024, 1 << 20, 10.0, 10.0)
    state.checkpoint(force=True)

    resumed = st.DriveState.load(tmp_path)
    assert resumed.max_temp_c == 58, \
        "peak temperature seen before an interruption must survive resume"


def test_observing_thermal_repeatedly_does_not_double_count_pauses(tmp_path):
    """_observe_thermal runs on every tick, so it must stay idempotent."""
    from driveprep import pipeline as pipe, thermal

    class Opts:
        chunk_size = None
        test_mode = True
        output_root = str(tmp_path)
        seller_name = ""
        mask_serial = False
        verbose = False
        skip_extended_test = False

    state = _state(tmp_path)
    disk = type("D", (), {"logical_block_bytes": 512,
                          "physical_block_bytes": 512, "id": "dp-test",
                          "size_bytes": 1 << 20})()
    p = pipe.DrivePipeline(disk, state, {"io": {}, "checkpoint": {}}, Opts())

    tstate = thermal.ThermalState(max_temp_c=52, paused_seconds=90.0)
    for _ in range(25):
        p._observe_thermal(tstate)
    assert p.thermal_state.paused_seconds == 0.0, \
        "live observation must not accumulate the pause counter"
    assert p.thermal_state.max_temp_c == 52

    p._absorb_thermal(tstate)          # end of pass: counted exactly once
    assert p.thermal_state.paused_seconds == 90.0


# --------------------------------------------------------------------------
# Phase transitions must restart, not resume (found on real hardware)
# --------------------------------------------------------------------------


def _pipeline(tmp_path, state, capacity):
    from driveprep import pipeline as pipe

    class Opts:
        chunk_size = 8 * 1024 * 1024
        test_mode = True
        output_root = str(tmp_path)
        seller_name = ""
        mask_serial = False
        verbose = False
        skip_extended_test = False

    disk = type("D", (), {"logical_block_bytes": 512,
                          "physical_block_bytes": 512, "id": "dp-test",
                          "size_bytes": capacity})()
    return pipe.DrivePipeline(disk, state, {"io": {}, "checkpoint": {}}, Opts())


def test_verify_starts_at_zero_after_a_completed_erase(tmp_path):
    """The bug that made a 500 GB verify read only its final 4 MB tail.

    phase_offset still held the erase's end position, so phase 5 "resumed"
    from the end of the device -- and because verify_zero sets
    bytes_done = start_offset on entry, the run then reported the full
    capacity as verified. A fabricated sanitization claim.
    """
    capacity = 500_107_862_016
    state = _state(tmp_path, capacity_bytes=capacity)
    state.phase = st.PHASE_ERASE
    state.phase_offset = capacity          # erase just finished
    p = _pipeline(tmp_path, state, capacity)

    assert p._start_offset_for(st.PHASE_VERIFY) == 0, \
        "entering a new phase must start at the beginning of the device"


def test_a_genuinely_interrupted_phase_still_resumes(tmp_path):
    """The fix must not break real resume: same phase -> keep the offset."""
    capacity = 64 * 1024 * 1024
    state = _state(tmp_path, capacity_bytes=capacity)
    state.phase = st.PHASE_VERIFY
    state.phase_offset = 26 * 1024 * 1024
    p = _pipeline(tmp_path, state, capacity)

    assert p._start_offset_for(st.PHASE_VERIFY) == 24 * 1024 * 1024, \
        "resuming the same phase keeps its offset, rounded to a chunk"


def test_erase_also_restarts_rather_than_inheriting(tmp_path):
    capacity = 64 * 1024 * 1024
    state = _state(tmp_path, capacity_bytes=capacity)
    state.phase = st.PHASE_SHORT_TEST
    state.phase_offset = 999_999
    p = _pipeline(tmp_path, state, capacity)
    assert p._start_offset_for(st.PHASE_ERASE) == 0


@pytest.mark.root
@needs_losetup
def test_verify_after_erase_actually_reads_the_whole_device():
    """End to end on a real device: erase then verify must cover all of it."""
    total = 32 * 1024 * 1024
    chunk = 1024 * 1024
    with loop_device(total) as loop:
        cfg = blockio.PassConfig(chunk_bytes=chunk, logical_block_bytes=512,
                                 physical_block_bytes=512,
                                 progress_interval_s=0)
        seed_pattern(loop, 0, total)

        fd = os.open(loop.path, os.O_RDWR | os.O_DIRECT)
        try:
            erased = blockio.zero_fill(fd, total, cfg)
        finally:
            os.close(fd)
        assert erased.bytes_done == total

        # Starting the verify at the erase's end offset is the bug: it reads
        # nothing and still reports full coverage. Counting progress ticks is
        # what distinguishes a real pass from a no-op -- bytes_done cannot,
        # which is precisely why the bug was invisible in the report.
        bogus_ticks = []
        fd = os.open(loop.path, os.O_RDONLY | os.O_DIRECT)
        try:
            bogus = blockio.verify_zero(fd, total, cfg,
                                        start_offset=erased.bytes_done,
                                        progress=lambda *a: bogus_ticks.append(a))
        finally:
            os.close(fd)
        assert bogus_ticks == [], "nothing was actually read"
        assert bogus.bytes_done == total, "yet bytes_done reports full coverage"

        # Starting at 0, as the fix does, genuinely reads the device.
        real_ticks = []
        fd = os.open(loop.path, os.O_RDONLY | os.O_DIRECT)
        try:
            real = blockio.verify_zero(fd, total, cfg, start_offset=0,
                                       progress=lambda *a: real_ticks.append(a))
        finally:
            os.close(fd)
        assert real.bytes_done == total
        assert len(real_ticks) == total // chunk, "every chunk was read"
        assert real.nonzero_ranges == []
