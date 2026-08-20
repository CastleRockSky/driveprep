"""Block I/O: spec 14 tests 1, 2, 3, 8b, 8c."""

from __future__ import annotations

import errno
import mmap
import os

import pytest

from driveprep import blockio
from make_loop import loop_device, seed_pattern

from conftest import needs_losetup


# --------------------------------------------------------------------------
# Test 8c: the zero comparison rule
# --------------------------------------------------------------------------


def test_8c_mmap_compares_false_memoryview_compares_true():
    """Pins the spec 9 comparison rule.

    Written the obvious way -- `mmap_obj == bytes(CHUNK)` -- every sector on
    every drive reads as nonzero and EVERY DRIVE GRADES FAIL. This test exists
    so nobody can quietly reintroduce that.
    """
    assert (mmap.mmap(-1, 16) == bytes(16)) is False
    assert (memoryview(mmap.mmap(-1, 16)) == bytes(16)) is True


def test_8c_is_zero_helper_uses_the_right_comparison():
    buf = mmap.mmap(-1, 4096)
    zero = bytes(4096)
    assert blockio.is_zero(buf, 4096, zero) is True
    buf[100] = 0xAB
    assert blockio.is_zero(buf, 4096, zero) is False


def test_8c_tail_chunk_slices_both_sides():
    """A short tail must compare equal, not mismatch on length alone."""
    buf = mmap.mmap(-1, 4096)
    zero = bytes(4096)
    assert blockio.is_zero(buf, 100, zero) is True
    # The unsliced form is the bug this guards against.
    assert (memoryview(buf) == zero[:100]) is False


# --------------------------------------------------------------------------
# Test 8b: transfer primitives
# --------------------------------------------------------------------------


def test_8b_os_read_write_fail_on_o_direct(tmp_path):
    """os.read/os.write raise EINVAL on an O_DIRECT fd; preadv/pwritev do not.

    CPython's bytes payload is not page aligned, so only the vectored calls
    writing into and out of an mmap satisfy O_DIRECT's alignment requirement.
    A future refactor must not quietly reintroduce the unaligned path.
    """
    path = tmp_path / "odirect.img"
    path.write_bytes(bytes(1 << 20))
    try:
        fd = os.open(path, os.O_RDWR | os.O_DIRECT)
    except OSError as exc:
        pytest.skip(f"O_DIRECT unsupported on this filesystem: {exc}")

    try:
        # Unaligned length and unaligned offset are rejected by the kernel
        # unconditionally. The buffer-alignment failure is the one this file
        # exists for, but it cannot be asserted directly: a 4096-byte bytes
        # object is served by mmap and IS page aligned often enough that
        # os.read(fd, 4096) intermittently SUCCEEDS. Asserting on that is
        # asserting on allocator luck, and it flaked in CI exactly once --
        # which is the worst frequency for a test guarding a destructive path.
        with pytest.raises(OSError) as caught:
            os.pread(fd, 511, 0)
        assert caught.value.errno == errno.EINVAL

        with pytest.raises(OSError) as caught:
            os.pread(fd, 4096, 1)
        assert caught.value.errno == errno.EINVAL

        # A deliberately misaligned buffer: slicing into the middle of a
        # larger allocation guarantees the payload is not page aligned.
        unaligned = memoryview(bytearray(8192))[1:4097]
        with pytest.raises(OSError) as caught:
            os.preadv(fd, [unaligned], 0)
        assert caught.value.errno == errno.EINVAL

        # And the path the code actually uses works.
        buf = mmap.mmap(-1, 4096)
        try:
            assert os.preadv(fd, [buf], 0) == 4096
            assert os.pwritev(fd, [buf], 0) == 4096
        finally:
            buf.close()
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# Geometry and LBA maths (test 3's arithmetic half)
# --------------------------------------------------------------------------


def test_chunking_never_writes_past_the_end():
    total = 20 * 1024 * 1024 + 4096
    chunks = list(blockio.iter_chunks(total, 8 * 1024 * 1024, 4096))
    assert sum(length for _o, length in chunks) == total
    assert all(offset + length <= total for offset, length in chunks)


def test_lba_uses_logical_not_physical_block_size():
    """Using the physical size makes every LBA wrong by 8x on 4Kn."""
    offset = 8 * 1024 * 1024
    at_512 = blockio.Range(offset, 4096, 512)
    at_4096 = blockio.Range(offset, 4096, 4096)
    assert at_4096.first_lba == 2048
    assert at_512.first_lba == 16384
    assert at_512.first_lba == at_4096.first_lba * 8


def test_chunk_must_be_a_multiple_of_both_block_sizes():
    assert blockio.validate_chunk(8 << 20, 4096, 4096) == 8 << 20
    with pytest.raises(blockio.BlockIOError, match="logical block size"):
        blockio.validate_chunk(1000, 4096, 4096)


def test_throughput_summary_reports_min_mean_max():
    """Throughput is reported but never graded -- SMR swings are not faults."""
    assert blockio.summarize_throughput([96.1, 144.6, 171.3]) == {
        "min_mbs": 96.1, "mean_mbs": 137.3, "max_mbs": 171.3}
    assert blockio.summarize_throughput([])["mean_mbs"] is None


def test_narrowing_finds_exact_nonzero_ranges():
    buf = mmap.mmap(-1, 8192)
    buf[0:512] = b"\xff" * 512
    buf[4096:4608] = b"\xff" * 512
    ranges = blockio.first_nonzero_ranges(buf, 8192, 512_000, 512, bytes(8192))
    assert [(r.start, r.length) for r in ranges] == [(512_000, 512), (516_096, 512)]
    assert ranges[0].first_lba == 1000


def test_progress_is_rate_limited_by_default(tmp_path):
    """Once per second in production, so a 20-hour run is not I/O-bound on it.

    This is correct behaviour, but it makes any test that drives should_stop()
    from the progress callback silently useless on a fast fixture: the callback
    never fires, nothing stops, and a complete run is mistaken for an
    interrupted one. Tests that need per-chunk progress must pass
    progress_interval_s=0. Pinned here because three root tests were written
    without it and all three passed vacuously.
    """
    path = tmp_path / "rate.img"
    path.write_bytes(bytes(16 * 1024 * 1024))
    try:
        fd = os.open(path, os.O_RDWR | os.O_DIRECT)
    except OSError as exc:
        pytest.skip(f"O_DIRECT unsupported: {exc}")

    cfg = blockio.PassConfig(chunk_bytes=1024 * 1024, logical_block_bytes=512,
                             physical_block_bytes=512)
    assert cfg.progress_interval_s == 1.0

    calls = []
    try:
        blockio.zero_fill(fd, 16 * 1024 * 1024, cfg,
                          progress=lambda *a: calls.append(a))
    finally:
        os.close(fd)
    assert calls == [], "a sub-second pass must not fire the rate-limited callback"


def test_progress_fires_every_chunk_at_zero_interval(tmp_path):
    path = tmp_path / "rate0.img"
    path.write_bytes(bytes(16 * 1024 * 1024))
    try:
        fd = os.open(path, os.O_RDWR | os.O_DIRECT)
    except OSError as exc:
        pytest.skip(f"O_DIRECT unsupported: {exc}")

    cfg = blockio.PassConfig(chunk_bytes=1024 * 1024, logical_block_bytes=512,
                             physical_block_bytes=512, progress_interval_s=0)
    calls = []
    try:
        blockio.zero_fill(fd, 16 * 1024 * 1024, cfg,
                          progress=lambda *a: calls.append(a))
    finally:
        os.close(fd)
    assert len(calls) == 16, "one callback per chunk"


def test_should_stop_halts_the_pass_at_a_chunk_boundary(tmp_path):
    """The mechanism the root interruption tests depend on."""
    path = tmp_path / "stop.img"
    total = 16 * 1024 * 1024
    path.write_bytes(bytes(total))
    try:
        fd = os.open(path, os.O_RDWR | os.O_DIRECT)
    except OSError as exc:
        pytest.skip(f"O_DIRECT unsupported: {exc}")

    cfg = blockio.PassConfig(chunk_bytes=1024 * 1024, logical_block_bytes=512,
                             physical_block_bytes=512, progress_interval_s=0)
    state = {"stop": False}
    try:
        findings = blockio.zero_fill(
            fd, total, cfg,
            progress=lambda done, *a: state.__setitem__(
                "stop", done >= total * 0.4),
            should_stop=lambda: state["stop"])
    finally:
        os.close(fd)

    assert 0 < findings.bytes_done < total, "must stop partway"
    assert findings.bytes_done % cfg.chunk_bytes == 0, "on a chunk boundary"


def test_findings_cap_recorded_ranges():
    """A totally failed drive must not produce a gigabyte of JSON."""
    findings = blockio.Findings()
    for i in range(1500):
        findings.record_error(blockio.Range(i * 512, 512, 512), cap=1000)
    assert len(findings.read_error_ranges) == 1000
    assert findings.read_errors == 1500
    assert findings.ranges_truncated is True


def test_findings_round_trip_through_json():
    """Findings must survive checkpoint and re-hydration (spec 9.1)."""
    findings = blockio.Findings()
    findings.record_error(blockio.Range(4096, 512, 512), cap=1000)
    findings.record_nonzero(blockio.Range(8192, 1024, 512), cap=1000)
    findings.bytes_done = 99999
    restored = blockio.Findings.from_json(findings.to_json(), 512)
    assert restored.read_errors == 1
    assert restored.nonzero_sectors == 2
    assert restored.bytes_done == 99999
    assert restored.read_error_ranges[0].start == 4096
    assert restored.nonzero_ranges[0].first_lba == 16


# --------------------------------------------------------------------------
# Tests 1, 2, 3 on real loop devices
# --------------------------------------------------------------------------


@pytest.mark.root
@needs_losetup
def test_1_zero_fill_end_to_end_reports_all_zero():
    with loop_device(64 * 1024 * 1024) as loop:
        cfg = blockio.PassConfig(chunk_bytes=1 << 20, logical_block_bytes=512,
                                 physical_block_bytes=512)
        seed_pattern(loop, 0, 1 << 20)

        fd = os.open(loop.path, os.O_RDWR | os.O_DIRECT)
        try:
            blockio.zero_fill(fd, loop.size_bytes, cfg)
        finally:
            os.close(fd)

        fd = os.open(loop.path, os.O_RDONLY | os.O_DIRECT)
        try:
            findings = blockio.verify_zero(fd, loop.size_bytes, cfg)
        finally:
            os.close(fd)

        assert findings.nonzero_ranges == []
        assert findings.read_errors == 0
        assert findings.bytes_done == loop.size_bytes


@pytest.mark.root
@needs_losetup
def test_2_verify_detects_nonzero_at_correct_offsets_and_lbas():
    with loop_device(64 * 1024 * 1024) as loop:
        cfg = blockio.PassConfig(chunk_bytes=1 << 20, logical_block_bytes=512,
                                 physical_block_bytes=512)
        seed_pattern(loop, 2 * 1024 * 1024, 512)
        seed_pattern(loop, 10 * 1024 * 1024, 1024)

        fd = os.open(loop.path, os.O_RDONLY | os.O_DIRECT)
        try:
            findings = blockio.verify_zero(fd, loop.size_bytes, cfg)
        finally:
            os.close(fd)

        found = {(r.start, r.length, r.first_lba) for r in findings.nonzero_ranges}
        assert (2 * 1024 * 1024, 512, 4096) in found
        assert (10 * 1024 * 1024, 1024, 20480) in found


@pytest.mark.root
@needs_losetup
def test_3_4k_logical_blocks_and_a_tail_that_is_not_a_chunk_multiple():
    """LBA maths at 4096, plus a device length that is not a chunk multiple."""
    size = 64 * 1024 * 1024 + 4096
    with loop_device(size, sector_size=4096) as loop:
        assert loop.size_bytes == size
        cfg = blockio.PassConfig(chunk_bytes=1 << 20, logical_block_bytes=4096,
                                 physical_block_bytes=4096)
        seed_pattern(loop, size - 4096, 4096)

        fd = os.open(loop.path, os.O_RDONLY | os.O_DIRECT)
        try:
            findings = blockio.verify_zero(fd, loop.size_bytes, cfg)
        finally:
            os.close(fd)

        assert len(findings.nonzero_ranges) == 1
        tail = findings.nonzero_ranges[0]
        assert tail.start == size - 4096
        assert tail.first_lba == (size - 4096) // 4096

        fd = os.open(loop.path, os.O_RDWR | os.O_DIRECT)
        try:
            blockio.zero_fill(fd, loop.size_bytes, cfg)
        finally:
            os.close(fd)

        # Nothing written past the end.
        assert loop.size_bytes == size
        assert loop.read_at(size - 4096, 4096) == bytes(4096)


@pytest.mark.root
@needs_losetup
def test_device_size_cross_check_catches_disagreement():
    with loop_device(32 * 1024 * 1024) as loop:
        fd = os.open(loop.path, os.O_RDONLY)
        try:
            assert blockio.device_size(fd) == loop.size_bytes
            assert blockio.check_size_agreement(fd, loop.size_bytes) == loop.size_bytes
            with pytest.raises(blockio.BlockIOError, match="disagreement"):
                blockio.check_size_agreement(fd, loop.size_bytes + 4096)
        finally:
            os.close(fd)


# --------------------------------------------------------------------------
# A write error must arrive as itself, not as a buffer-protocol error
# --------------------------------------------------------------------------


def _eio_at(fail_offset):
    """os.pwritev that succeeds until fail_offset, then raises EIO."""
    real = os.pwritev

    def fake(fd, buffers, offset):
        if offset >= fail_offset:
            raise OSError(errno.EIO, "Input/output error")
        return real(fd, buffers, offset)

    return fake


def test_write_eio_is_not_masked_by_the_buffer_close(tmp_path, monkeypatch):
    """Regression: two 2 TB drives failed at 1.33 TB and the reason was lost.

    _pwrite_full held `view = memoryview(buf)[...]` in a local. When pwritev
    raised, the view stayed alive, so the mmap still had an exported pointer
    when zero_fill's finally block called buf.close() -- which raises
    BufferError, REPLACING the BlockIOError on its way out. Both drives
    recorded:

        failed_reason: BufferError: cannot close exported pointers exist

    instead of the EIO and the offset, which is the only part an operator can
    act on. The errno tells you whether to suspect the media or the cable; the
    offset tells you where it stopped.
    """
    path = tmp_path / "disk.img"
    path.write_bytes(b"\xff" * (4 << 20))
    cfg = blockio.PassConfig(chunk_bytes=1 << 20, logical_block_bytes=512,
                             physical_block_bytes=512)

    monkeypatch.setattr(os, "pwritev", _eio_at(2 << 20))
    fd = os.open(path, os.O_RDWR)
    try:
        with pytest.raises(blockio.BlockIOError) as caught:
            blockio.zero_fill(fd, 4 << 20, cfg)
    finally:
        os.close(fd)

    assert "2097152" in str(caught.value), "the offset must survive"
    assert "Input/output error" in str(caught.value), "the errno must survive"
    assert caught.value.__cause__.errno == errno.EIO
    assert not isinstance(caught.value, BufferError)


def test_read_eio_is_recorded_and_the_pass_continues(tmp_path, monkeypatch):
    """The read path absorbs an EIO rather than raising (spec 9).

    This is the behaviour the write path does NOT have and must not grow: a
    bad sector is a finding, a failed write is fatal. Not a leak regression --
    _pread_full rebinds its view each chunk, so a stale one is collected before
    the close.
    """
    path = tmp_path / "disk.img"
    path.write_bytes(bytes(4 << 20))
    cfg = blockio.PassConfig(chunk_bytes=1 << 20, logical_block_bytes=512,
                             physical_block_bytes=512)

    real = os.preadv

    def fake(fd, buffers, offset):
        if offset == (1 << 20):
            raise OSError(errno.EIO, "Input/output error")
        return real(fd, buffers, offset)

    monkeypatch.setattr(os, "preadv", fake)
    fd = os.open(path, os.O_RDONLY)
    try:
        findings = blockio.verify_zero(fd, 4 << 20, cfg)
    finally:
        os.close(fd)

    assert findings.read_errors >= 1, "the error is recorded, not raised"
    assert findings.bytes_done == 4 << 20, "and the pass still completes"


def test_the_transfer_buffer_has_no_exported_views_after_an_error(monkeypatch):
    """Pins the mechanism directly, independent of either caller.

    The exception is bound so its traceback keeps _pwrite_full's frame -- and
    therefore any leaked view -- alive. That is what the real caller does with
    `except Exception as exc`, and without it the frame is collected early and
    a leak escapes the test.
    """
    buf = mmap.mmap(-1, 1 << 20)
    monkeypatch.setattr(os, "pwritev", _eio_at(0))
    try:
        blockio._pwrite_full(3, buf, 0, 1 << 20)
    except OSError as exc:
        held = exc          # noqa: F841 -- holds the traceback, as the caller does
        buf.close()         # raises BufferError if a view leaked
    else:
        pytest.fail("the fake pwritev should have raised")
