"""losetup fixtures (spec 14).

No test may target a physical disk. A loop device is a real block device that
supports O_DIRECT, so it exercises the genuine code path rather than a mock.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path


class LoopFixture:
    def __init__(self, path: str, backing: Path, sector_size: int):
        self.path = path
        self.backing = backing
        self.sector_size = sector_size

    @property
    def kname(self) -> str:
        return Path(self.path).name

    @property
    def size_bytes(self) -> int:
        return int(Path(f"/sys/block/{self.kname}/size").read_text().strip()) * 512

    def write_at(self, offset: int, data: bytes) -> None:
        """Seed data through the backing file, bypassing the loop device."""
        with open(self.backing, "r+b") as handle:
            handle.seek(offset)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def read_at(self, offset: int, length: int) -> bytes:
        with open(self.backing, "rb") as handle:
            handle.seek(offset)
            return handle.read(length)


@contextmanager
def loop_device(size_bytes: int = 512 * 1024 * 1024, sector_size: int = 512,
                partitioned: bool = False):
    """Create a loop device backed by a sparse file, and clean it up.

    `partitioned` passes -P so the kernel actually creates partition nodes.
    Without it, a partition-based fixture silently tests nothing, because
    /sys/class/block/loopNp1 never appears.
    """
    handle, backing_path = tempfile.mkstemp(prefix="dp-test-", suffix=".img")
    os.close(handle)
    backing = Path(backing_path)

    try:
        subprocess.run(["truncate", "-s", str(size_bytes), str(backing)],
                       check=True, capture_output=True)
        cmd = ["losetup", "--find", "--show", "--sector-size", str(sector_size)]
        if partitioned:
            cmd.append("-P")
        cmd.append(str(backing))
        device = subprocess.run(cmd, check=True, capture_output=True,
                                text=True).stdout.strip()
        try:
            yield LoopFixture(device, backing, sector_size)
        finally:
            subprocess.run(["losetup", "-d", device], check=False,
                           capture_output=True)
    finally:
        backing.unlink(missing_ok=True)


def seed_pattern(fixture: LoopFixture, offset: int, length: int,
                 pattern: bytes = b"\xde\xad\xbe\xef") -> None:
    repeats = (length // len(pattern)) + 1
    fixture.write_at(offset, (pattern * repeats)[:length])


def make_partition(fixture: LoopFixture) -> str:
    """Create a single partition and return its kernel name."""
    subprocess.run(
        ["sfdisk", fixture.path],
        input="label: dos\nstart=2048, type=83\n",
        text=True, check=True, capture_output=True,
    )
    subprocess.run(["partprobe", fixture.path], check=False, capture_output=True)
    subprocess.run(["blockdev", "--rereadpt", fixture.path], check=False,
                   capture_output=True)
    return f"{fixture.kname}p1"
