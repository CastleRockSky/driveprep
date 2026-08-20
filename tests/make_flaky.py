"""device-mapper fixtures for the unhappy paths (spec 14).

A dm table that maps most of the range to a loop device and a slice of it to
`error` produces genuine EIO at known offsets. This is the only realistic way
to test the read-error-range logic, and it is the main reason this build is
more testable than the Windows equivalent would have been.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path


class DmFixture:
    def __init__(self, name: str, bad_ranges: list[tuple[int, int]],
                 sector_size: int = 512):
        self.name = name
        self.bad_ranges = bad_ranges  # (start_byte, length_byte)
        self.sector_size = sector_size

    @property
    def path(self) -> str:
        return f"/dev/mapper/{self.name}"

    @property
    def kname(self) -> str:
        return Path(f"/dev/mapper/{self.name}").resolve().name

    @property
    def size_bytes(self) -> int:
        return int(Path(f"/sys/block/{self.kname}/size").read_text().strip()) * 512


def _dmsetup(args: list[str], table: str | None = None):
    return subprocess.run(["dmsetup", *args], input=table, text=True,
                          check=True, capture_output=True)


@contextmanager
def dm_error_device(loop_path: str, total_bytes: int,
                    bad_slices: list[tuple[int, int]],
                    name: str = "driveprep-test-flaky"):
    """Map `loop_path` with `bad_slices` (start_byte, length_byte) as errors.

    Sectors are dm's 512-byte units regardless of the device's logical block
    size, which is the same convention as /sys/block/<k>/size.
    """
    total_sectors = total_bytes // 512
    rows = []
    cursor = 0

    for start, length in sorted(bad_slices):
        start_sector, length_sectors = start // 512, length // 512
        if start_sector > cursor:
            rows.append(f"{cursor} {start_sector - cursor} linear "
                        f"{loop_path} {cursor}")
        rows.append(f"{start_sector} {length_sectors} error")
        cursor = start_sector + length_sectors

    if cursor < total_sectors:
        rows.append(f"{cursor} {total_sectors - cursor} linear "
                    f"{loop_path} {cursor}")

    table = "\n".join(rows) + "\n"
    _dmsetup(["create", name], table)
    try:
        yield DmFixture(name, bad_slices)
    finally:
        subprocess.run(["dmsetup", "remove", "--force", name],
                       check=False, capture_output=True)
