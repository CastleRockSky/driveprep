"""O_DIRECT block I/O: zero fill and full-surface verification (spec 9).

Standard library only, at the os level. Several details here are mandatory
rather than stylistic, and each is load-bearing:

  * O_DIRECT is required. Without it the verification read is served from the
    page cache, which makes the verify partly circular and the throughput
    numbers fictional.

  * The buffer is an anonymous mmap. It is page aligned for free, which
    satisfies O_DIRECT's alignment requirement with no ctypes or
    posix_memalign work. This is the single biggest simplification Linux buys
    here.

  * Transfers use os.preadv / os.pwritev. os.read(fd, n) and os.write(fd, b)
    both fail with EINVAL on an O_DIRECT descriptor, because CPython's bytes
    payload is not page aligned. Only the vectored calls writing into and out
    of the mmap satisfy alignment. Positional preadv/pwritev also remove any
    dependence on the file offset, which makes resume and retry-at-offset
    trivial.

  * The zero comparison is memoryview(buf)[:n] == ZERO_BLOCK[:n]. Both halves
    matter; see is_zero() below.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import mmap
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from . import log

_log = log.get("blockio")

BLKGETSIZE64 = 0x80081272

DEFAULT_CHUNK = 8 * 1024 * 1024


class BlockIOError(RuntimeError):
    """Fatal I/O condition. Never a media error; see WriteProtectedError."""


class WriteProtectedError(BlockIOError):
    """EROFS/EACCES/EPERM on write (spec 4.6).

    These mean the device or its bridge is write-protected, not that the
    platters are bad. They must never fall into the record-and-continue path,
    or a configuration problem gets reported as a drive with millions of bad
    sectors.
    """


class DeviceVanishedError(BlockIOError):
    """ENODEV/ENXIO. Usually a USB drop; the caller may wait and reconnect."""


# --------------------------------------------------------------------------
# Device geometry
# --------------------------------------------------------------------------


def device_size(fd: int) -> int:
    """Device length in bytes via BLKGETSIZE64."""
    buf = ctypes.c_uint64(0)
    fcntl.ioctl(fd, BLKGETSIZE64, buf)
    return buf.value


def check_size_agreement(fd: int, sysfs_size_bytes: int) -> int:
    """Cross-check BLKGETSIZE64 against sysfs and abort on disagreement.

    Remember that /sys/block/<kname>/size is always in 512-byte units even on
    a 4Kn drive; identity.size_bytes_of() is the only place that conversion
    happens, and it multiplies by 512, never by the logical block size.
    """
    ioctl_size = device_size(fd)
    if ioctl_size != sysfs_size_bytes:
        raise BlockIOError(
            f"device length disagreement: BLKGETSIZE64 reports {ioctl_size:,} "
            f"bytes, sysfs reports {sysfs_size_bytes:,}. Refusing to write to a "
            f"device whose length is uncertain."
        )
    return ioctl_size


def validate_chunk(chunk: int, logical: int, physical: int) -> int:
    """Chunk size must be an exact multiple of both block sizes (spec 9)."""
    if chunk <= 0:
        raise BlockIOError(f"chunk size must be positive, got {chunk}")
    for name, size in (("logical", logical), ("physical", physical)):
        if size and chunk % size:
            raise BlockIOError(
                f"chunk size {chunk} is not a multiple of the {name} block size "
                f"{size}. O_DIRECT transfers must be block aligned."
            )
    return chunk


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


@dataclass
class Range:
    """A byte range, with LBAs derived at the LOGICAL block size.

    Using the physical size makes every reported LBA wrong by 8x on an
    advanced-format drive, which is exactly the kind of error a buyer will
    catch. (lba_of_first_error in the self-test blocks comes from smartctl and
    is passed through unmodified; it is not computed here.)
    """

    start: int
    length: int
    logical_block_bytes: int = 512

    @property
    def end(self) -> int:
        return self.start + self.length

    @property
    def first_lba(self) -> int:
        return self.start // self.logical_block_bytes

    @property
    def last_lba(self) -> int:
        return (self.end - 1) // self.logical_block_bytes

    def to_json(self) -> dict:
        return {
            "start_byte": self.start,
            "length_bytes": self.length,
            "first_lba": self.first_lba,
            "last_lba": self.last_lba,
        }


@dataclass
class Findings:
    """Accumulated results for one pass.

    These are checkpointed and re-hydrated on resume (spec 9.1). If they are
    not, a run interrupted at 40 percent that had already hit 30 read errors
    resumes with a clean slate and grades PASS on a failing drive.
    """

    read_error_ranges: list[Range] = field(default_factory=list)
    nonzero_ranges: list[Range] = field(default_factory=list)
    read_errors: int = 0
    nonzero_sectors: int = 0
    bytes_done: int = 0
    throughput_samples: list[float] = field(default_factory=list)
    disconnects: int = 0
    ranges_truncated: bool = False

    def record_error(self, rng: Range, cap: int) -> None:
        self.read_errors += 1
        if len(self.read_error_ranges) < cap:
            self.read_error_ranges.append(rng)
        else:
            self.ranges_truncated = True

    def record_nonzero(self, rng: Range, cap: int) -> None:
        self.nonzero_sectors += max(1, rng.length // rng.logical_block_bytes)
        if len(self.nonzero_ranges) < cap:
            self.nonzero_ranges.append(rng)
        else:
            self.ranges_truncated = True

    def to_json(self) -> dict:
        return {
            "read_error_ranges": [r.to_json() for r in self.read_error_ranges],
            "nonzero_ranges": [r.to_json() for r in self.nonzero_ranges],
            "read_errors": self.read_errors,
            "nonzero_sectors": self.nonzero_sectors,
            "bytes_done": self.bytes_done,
            "throughput_samples": self.throughput_samples,
            "disconnects": self.disconnects,
            "ranges_truncated": self.ranges_truncated,
        }

    @classmethod
    def from_json(cls, data: dict | None, logical: int = 512) -> "Findings":
        if not data:
            return cls()

        def ranges(key):
            return [
                Range(r["start_byte"], r["length_bytes"], logical)
                for r in data.get(key, [])
            ]

        return cls(
            read_error_ranges=ranges("read_error_ranges"),
            nonzero_ranges=ranges("nonzero_ranges"),
            read_errors=data.get("read_errors", 0),
            nonzero_sectors=data.get("nonzero_sectors", 0),
            bytes_done=data.get("bytes_done", 0),
            throughput_samples=list(data.get("throughput_samples", [])),
            disconnects=data.get("disconnects", 0),
            ranges_truncated=data.get("ranges_truncated", False),
        )


# --------------------------------------------------------------------------
# The zero comparison
# --------------------------------------------------------------------------


def is_zero(buf: mmap.mmap, n: int, zero_block: bytes) -> bool:
    """True if the first n bytes of buf are all zero.

    Both halves of this expression matter:

      memoryview(buf), not buf. An mmap object has no rich comparison against
      bytes, so `mmap_obj == bytes(CHUNK)` evaluates to False unconditionally.
      Written that way, every sector on every drive reads as nonzero and EVERY
      DRIVE GRADES FAIL.

      Slice to n on both sides. The tail chunk is short, so comparing a
      full-size ZERO_BLOCK against a partially filled buffer mismatches on
      length alone.

    CPython dispatches the sliced memoryview comparison to memcmp, which runs
    at memory bandwidth. Never loop per byte in Python; that alone would take
    longer than the disk read.
    """
    return memoryview(buf)[:n] == zero_block[:n]


def first_nonzero_ranges(
    buf: mmap.mmap, n: int, base_offset: int, logical: int, zero_block: bytes
) -> list[Range]:
    """Narrow a mismatching chunk to exact block-granular ranges."""
    view = memoryview(buf)[:n]
    out: list[Range] = []
    run_start: int | None = None
    for pos in range(0, n, logical):
        span = min(logical, n - pos)
        if view[pos:pos + span] == zero_block[:span]:
            if run_start is not None:
                out.append(Range(base_offset + run_start, pos - run_start, logical))
                run_start = None
        elif run_start is None:
            run_start = pos
    if run_start is not None:
        out.append(Range(base_offset + run_start, n - run_start, logical))
    return out


# --------------------------------------------------------------------------
# Transfer primitives
# --------------------------------------------------------------------------


def _pwrite_full(fd: int, buf: mmap.mmap, offset: int, length: int) -> int:
    """Write `length` bytes from buf at `offset`, looping on short writes.

    pwritev may return fewer bytes than requested, and reliably does on the
    tail chunk at end of device. A return of 0 before the expected end is an
    error, not EOF.
    """
    done = 0
    # Each view is released deterministically. Left alive by an exception, an
    # exported pointer makes the mmap's close() raise BufferError from the
    # finally block -- which then REPLACES the real error. A genuine EIO at
    # 1.3 TB was recorded as "BufferError: cannot close exported pointers
    # exist", losing both the errno and the offset.
    mv = memoryview(buf)
    try:
      while done < length:
        with mv[done:length] as view:
            written = os.pwritev(fd, [view], offset + done)
        if written == 0:
            raise BlockIOError(
                f"pwritev returned 0 at offset {offset + done} with "
                f"{length - done} bytes still to write"
            )
        done += written
    finally:
        mv.release()
    return done


def _pread_full(fd: int, buf: mmap.mmap, offset: int, length: int) -> int:
    """Read up to `length` bytes into buf at `offset`, looping on short reads.

    Returns the number of bytes actually read. A short read at the very end of
    the device is legitimate; a zero return before that is an error.
    """
    done = 0
    mv = memoryview(buf)
    try:
      while done < length:
        with mv[done:length] as view:
            got = os.preadv(fd, [view], offset + done)
        if got == 0:
            if done == 0:
                raise BlockIOError(
                    f"preadv returned 0 at offset {offset} before the expected "
                    f"end of device"
                )
            break
        done += got
    finally:
        mv.release()
    return done


# --------------------------------------------------------------------------
# Passes
# --------------------------------------------------------------------------


@dataclass
class PassConfig:
    chunk_bytes: int = DEFAULT_CHUNK
    logical_block_bytes: int = 512
    physical_block_bytes: int = 512
    max_recorded_ranges: int = 1000
    narrow_error_ranges: bool = True
    progress_interval_s: float = 1.0


ProgressCb = Callable[[int, int, float, float], None]
"""(bytes_done, total_bytes, instantaneous_mbs, mean_mbs)"""


def _clamp_chunk(offset: int, total: int, chunk: int, logical: int) -> int:
    """Length of the transfer at `offset`.

    The final chunk is clamped to the remaining byte count and rounded UP to
    the logical block size. Device length is always an exact multiple of the
    logical block size, so this never overruns.
    """
    remaining = total - offset
    if remaining >= chunk:
        return chunk
    if remaining % logical:
        # Should be unreachable on a real block device; kept as a guard so a
        # malformed fixture fails loudly rather than writing past the end.
        remaining += logical - (remaining % logical)
    return remaining


def zero_fill(
    fd: int,
    total_bytes: int,
    cfg: PassConfig,
    *,
    start_offset: int = 0,
    findings: Findings | None = None,
    progress: ProgressCb | None = None,
    should_stop: Callable[[], bool] | None = None,
    pause_gate: Callable[[], None] | None = None,
) -> Findings:
    """Single-pass 0x00 fill from start_offset to the end of the device.

    Covers LBA 0 through the last block, including any partition table, the GPT
    backup header at the end of the disk, and all slack (spec 5).
    """
    findings = findings or Findings()
    validate_chunk(cfg.chunk_bytes, cfg.logical_block_bytes, cfg.physical_block_bytes)

    buf = mmap.mmap(-1, cfg.chunk_bytes)  # anonymous => page aligned
    buf.seek(0)
    buf.write(bytes(cfg.chunk_bytes))     # all zeros

    offset = start_offset
    findings.bytes_done = start_offset
    started = time.monotonic()
    last_report = started
    last_bytes = start_offset

    try:
        while offset < total_bytes:
            if should_stop and should_stop():
                _log.info("zero fill stopping at %d on request", offset)
                break
            if pause_gate:
                pause_gate()

            length = _clamp_chunk(offset, total_bytes, cfg.chunk_bytes,
                                  cfg.logical_block_bytes)
            try:
                _pwrite_full(fd, buf, offset, length)
            except OSError as exc:
                _classify_write_error(exc, offset)

            offset += length
            findings.bytes_done = min(offset, total_bytes)

            now = time.monotonic()
            if progress and now - last_report >= cfg.progress_interval_s:
                inst = (findings.bytes_done - last_bytes) / (now - last_report) / 1e6
                mean = (findings.bytes_done - start_offset) / max(now - started, 1e-9) / 1e6
                findings.throughput_samples.append(round(inst, 2))
                progress(findings.bytes_done, total_bytes, inst, mean)
                last_report, last_bytes = now, findings.bytes_done

        # Commit any bridge-level cache before the verify begins. Required even
        # with O_DIRECT (spec 9).
        os.fsync(fd)
    finally:
        _close_buffer(buf)

    return findings


def _close_buffer(buf: mmap.mmap) -> None:
    """Close the transfer buffer without ever masking an in-flight exception.

    Called from a finally block, so a raise here would replace whatever real
    error is propagating -- the difference between an operator reading
    "write failed at offset 1332555546624: Input/output error" and reading a
    CPython buffer-protocol message that says nothing about their drive.
    """
    try:
        buf.close()
    except BufferError:
        _log.debug("buffer still had exported views at close; leaving to GC")


def _classify_write_error(exc: OSError, offset: int) -> None:
    """Map a write errno to the right failure class (spec 4.6, 9)."""
    if exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM):
        raise WriteProtectedError(
            f"write refused at offset {offset} with {errno.errorcode[exc.errno]}. "
            f"The device or its USB bridge is write-protected. This is a "
            f"configuration problem, not a media fault -- check for a physical "
            f"write-protect switch or a bridge in read-only mode."
        ) from exc
    if exc.errno in (errno.ENODEV, errno.ENXIO):
        raise DeviceVanishedError(
            f"device vanished at offset {offset} ({errno.errorcode[exc.errno]})"
        ) from exc
    raise BlockIOError(f"write failed at offset {offset}: {exc}") from exc


def verify_zero(
    fd: int,
    total_bytes: int,
    cfg: PassConfig,
    *,
    start_offset: int = 0,
    findings: Findings | None = None,
    progress: ProgressCb | None = None,
    should_stop: Callable[[], bool] | None = None,
    pause_gate: Callable[[], None] | None = None,
) -> Findings:
    """Full-surface read confirming every byte reads back as zero.

    An EIO at offset X does not abort the pass: the byte range is recorded, the
    failing chunk is skipped, and the pass continues, so the report can state
    total bad regions rather than "failed at 1.2 TB".
    """
    findings = findings or Findings()
    validate_chunk(cfg.chunk_bytes, cfg.logical_block_bytes, cfg.physical_block_bytes)

    buf = mmap.mmap(-1, cfg.chunk_bytes)
    zero_block = bytes(cfg.chunk_bytes)

    offset = start_offset
    findings.bytes_done = start_offset
    started = time.monotonic()
    last_report = started
    last_bytes = start_offset

    try:
        while offset < total_bytes:
            if should_stop and should_stop():
                _log.info("verify stopping at %d on request", offset)
                break
            if pause_gate:
                pause_gate()

            length = _clamp_chunk(offset, total_bytes, cfg.chunk_bytes,
                                  cfg.logical_block_bytes)
            try:
                got = _pread_full(fd, buf, offset, length)
            except OSError as exc:
                if exc.errno in (errno.ENODEV, errno.ENXIO):
                    raise DeviceVanishedError(
                        f"device vanished at offset {offset}"
                    ) from exc
                if exc.errno != errno.EIO:
                    raise BlockIOError(f"read failed at offset {offset}: {exc}") from exc

                bad = _narrow_read_error(fd, buf, offset, length, cfg) \
                    if cfg.narrow_error_ranges else [Range(offset, length,
                                                           cfg.logical_block_bytes)]
                for rng in bad:
                    findings.record_error(rng, cfg.max_recorded_ranges)
                _log.warning(
                    "read error at offset %d (%d byte region, %d sub-range(s)); "
                    "continuing", offset, length, len(bad),
                )
                offset += length
                findings.bytes_done = min(offset, total_bytes)
                continue

            if not is_zero(buf, got, zero_block):
                for rng in first_nonzero_ranges(buf, got, offset,
                                                cfg.logical_block_bytes, zero_block):
                    findings.record_nonzero(rng, cfg.max_recorded_ranges)

            offset += got
            findings.bytes_done = min(offset, total_bytes)

            now = time.monotonic()
            if progress and now - last_report >= cfg.progress_interval_s:
                inst = (findings.bytes_done - last_bytes) / (now - last_report) / 1e6
                mean = (findings.bytes_done - start_offset) / max(now - started, 1e-9) / 1e6
                findings.throughput_samples.append(round(inst, 2))
                progress(findings.bytes_done, total_bytes, inst, mean)
                last_report, last_bytes = now, findings.bytes_done
    finally:
        _close_buffer(buf)

    return findings


def _narrow_read_error(
    fd: int, buf: mmap.mmap, offset: int, length: int, cfg: PassConfig
) -> list[Range]:
    """Re-read a failing chunk at logical-block granularity, once.

    Narrows the bad region from 8 MiB to the actual sectors, which is the
    difference between a report that says "3 bad sectors" and one that says
    "24 MB unreadable".
    """
    logical = cfg.logical_block_bytes
    bad: list[Range] = []
    run_start: int | None = None

    for pos in range(0, length, logical):
        span = min(logical, length - pos)
        try:
            os.preadv(fd, [memoryview(buf)[:span]], offset + pos)
            ok = True
        except OSError as exc:
            if exc.errno in (errno.ENODEV, errno.ENXIO):
                raise DeviceVanishedError(
                    f"device vanished while narrowing at {offset + pos}"
                ) from exc
            ok = False
        if ok:
            if run_start is not None:
                bad.append(Range(offset + run_start, pos - run_start, logical))
                run_start = None
        elif run_start is None:
            run_start = pos

    if run_start is not None:
        bad.append(Range(offset + run_start, length - run_start, logical))
    return bad or [Range(offset, length, logical)]


def summarize_throughput(samples: list[float]) -> dict:
    """min / mean / max in MB/s.

    Throughput is reported but kept out of the grading rubric entirely: a full
    sequential write on SMR media is slow and shows large swings as the drive's
    media cache saturates, and that is normal rather than a fault (spec 5.3).
    """
    if not samples:
        return {"min_mbs": None, "mean_mbs": None, "max_mbs": None}
    return {
        "min_mbs": round(min(samples), 1),
        "mean_mbs": round(sum(samples) / len(samples), 1),
        "max_mbs": round(max(samples), 1),
    }


def estimate_duration(fd: int, total_bytes: int, cfg: PassConfig) -> float:
    """Read-only throughput probe for the phase-3 estimate (spec 7).

    The estimate must be computed WITHOUT WRITING ANYTHING: phase 3 is the
    confirmation gate, so any calibration write before it would put bytes on
    the platters -- including LBA 0 -- before the operator has confirmed.

    Reads 1 GB sequentially from the middle of the device and extrapolates.
    Returns estimated seconds for one full pass.
    """
    sample_bytes = min(1024 * 1024 * 1024, max(total_bytes // 8, cfg.chunk_bytes))
    sample_bytes -= sample_bytes % cfg.logical_block_bytes
    start = (total_bytes // 2) - (sample_bytes // 2)
    start -= start % cfg.logical_block_bytes
    start = max(0, start)

    buf = mmap.mmap(-1, cfg.chunk_bytes)
    read = 0
    began = time.monotonic()
    try:
        while read < sample_bytes:
            length = min(cfg.chunk_bytes, sample_bytes - read)
            try:
                got = _pread_full(fd, buf, start + read, length)
            except OSError:
                break
            if got == 0:
                break
            read += got
    finally:
        _close_buffer(buf)

    elapsed = time.monotonic() - began
    if read == 0 or elapsed <= 0:
        return 0.0
    rate = read / elapsed
    return total_bytes / rate if rate else 0.0


def iter_chunks(total_bytes: int, chunk: int, logical: int,
                start: int = 0) -> Iterator[tuple[int, int]]:
    """(offset, length) pairs covering the device. Used by tests."""
    offset = start
    while offset < total_bytes:
        length = _clamp_chunk(offset, total_bytes, chunk, logical)
        yield offset, length
        offset += length
