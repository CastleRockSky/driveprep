"""Duration estimate (spec 7).

Corrected after a real 2 x 4 TB batch was estimated at 21.7 h and took 28.6 h.
The estimate is what an operator plans their day around, so being wrong by
seven hours is a real defect even though nothing is unsafe about it.
"""

from __future__ import annotations

import pytest

from driveprep import __main__ as cli, blockio, safety, state as st


class Opts:
    def __init__(self, tmp_path, jobs=1, skip_extended=False):
        self.chunk_size = None
        self.test_mode = True
        self.output_root = str(tmp_path)
        self.jobs = jobs
        self.skip_extended_test = skip_extended


def _disk(name, size=4_000_787_030_016):
    """A REAL inv.Disk, not a stand-in.

    A stub built with type() is hashable; inv.Disk is a dataclass and so is
    not. Using a stub let _estimate_batch key a dict by the Disk object and
    pass every test, then fail with TypeError on the first real batch.
    """
    from driveprep import inventory as inv
    return inv.Disk(kname=name, by_id=name, synthetic_id=None, identity=None,
                    bus_type="usb", size_bytes=size, logical_block_bytes=512,
                    physical_block_bytes=512, sysfs_rotational=1,
                    read_only=False, device_class="scsi", model="M",
                    serial=name)


@pytest.fixture
def probe_stub(monkeypatch):
    """Replace the real device probe with a fixed per-pass duration."""
    import contextlib
    import threading
    import time
    calls = {"concurrent": 0, "peak": 0}
    lock = threading.Lock()

    @contextlib.contextmanager
    def fake_open(disk, identity, **kw):
        with lock:
            calls["concurrent"] += 1
            calls["peak"] = max(calls["peak"], calls["concurrent"])
        # Hold the "device" briefly, otherwise a probe finishes before the
        # next starts and genuine concurrency is indistinguishable from
        # serial execution.
        time.sleep(0.05)
        try:
            yield (0, disk.kname)
        finally:
            with lock:
                calls["concurrent"] -= 1

    monkeypatch.setattr(safety, "guarded_open", fake_open)
    monkeypatch.setattr(blockio, "estimate_duration",
                        lambda fd, size, cfg: 8 * 3600.0)   # 8 h per pass
    return calls


def _state(tmp_path, name, extended_minutes=None):
    data = None
    if extended_minutes is not None:
        data = {"ata_smart_data": {"self_test": {
            "polling_minutes": {"short": 2, "extended": extended_minutes}}}}
    return st.DriveState(drive_id=name, output_dir=tmp_path, batch_id="B",
                         run_id="R", smartctl_d_type="sat",
                         smart_before_data=data)


def test_probes_run_concurrently_when_jobs_allows(tmp_path, probe_stub):
    """Sequential probes measure uncontended speed the run never achieves."""
    disks = [_disk("a"), _disk("b")]
    states = {d.id: _state(tmp_path, d.id, 474) for d in disks}
    cli._estimate_batch(disks, states, {"io": {}}, Opts(tmp_path, jobs=2))
    assert probe_stub["peak"] == 2, "both probes must overlap under --jobs 2"


def test_probes_are_serialised_at_jobs_one(tmp_path, probe_stub):
    disks = [_disk("a"), _disk("b")]
    states = {d.id: _state(tmp_path, d.id, 474) for d in disks}
    cli._estimate_batch(disks, states, {"io": {}}, Opts(tmp_path, jobs=1))
    assert probe_stub["peak"] == 1


def test_extended_test_uses_the_drives_own_estimate(tmp_path, probe_stub):
    """474 min is known exactly; assuming another 8 h pass was guesswork."""
    disks = [_disk("a")]
    states = {"a": _state(tmp_path, "a", 474)}
    total = cli._estimate_batch(disks, states, {"io": {}}, Opts(tmp_path))
    assert total == pytest.approx(8 * 3600 * 2 + 474 * 60)


def test_a_single_drive_gets_a_point_estimate(tmp_path, probe_stub):
    """With one drive there is no contention, so a range would be noise."""
    disks = [_disk("a")]
    states = {"a": _state(tmp_path, "a", 474)}
    assert not isinstance(
        cli._estimate_batch(disks, states, {"io": {}}, Opts(tmp_path)), tuple)


def test_contended_drives_get_a_range(tmp_path, monkeypatch):
    """A point estimate was 16% out either way; the range must contain reality."""
    import contextlib

    @contextlib.contextmanager
    def fake_open(disk, identity, **kw):
        yield (0, disk.kname)

    # Probe order: solo a, solo b, then both together.
    seq = iter([8 * 3600.0, 8 * 3600.0, 12.64 * 3600, 12.64 * 3600])
    monkeypatch.setattr(safety, "guarded_open", fake_open)
    monkeypatch.setattr(blockio, "estimate_duration",
                        lambda fd, size, cfg: next(seq))

    disks = [_disk("a"), _disk("b")]
    states = {d.id: _state(tmp_path, d.id, 474) for d in disks}
    result = cli._estimate_batch(disks, states, {"io": {}}, Opts(tmp_path, jobs=2))

    assert isinstance(result, tuple), "contended batches report a range"
    low, high = (v / 3600 for v in result)
    assert low == pytest.approx(24.0, abs=0.2)
    assert high == pytest.approx(33.2, abs=0.2)
    assert low < 28.6 < high, "the real 28.6 h outcome must fall inside"


def test_the_manifest_renders_a_range(tmp_path):
    from driveprep import inventory as inv, safety as S
    disk = inv.Disk(kname="sdz", by_id="usb-X-0:0", synthetic_id=None,
                    identity=None, bus_type="usb", size_bytes=1 << 40,
                    logical_block_bytes=512, physical_block_bytes=512,
                    sysfs_rotational=1, read_only=False, device_class="scsi",
                    model="X", serial="S")
    text = S.Manifest([disk], "DP-1-0000", 1 << 40,
                      (24 * 3600, 33.2 * 3600)).render()
    assert "24.0 to 33.2 hours" in text
    text = S.Manifest([disk], "DP-1-0000", 1 << 40, 12 * 3600).render()
    assert "12.0 hours" in text


def test_extended_test_falls_back_to_a_pass_when_unknown(tmp_path, probe_stub):
    disks = [_disk("a")]
    states = {"a": _state(tmp_path, "a", None)}
    total = cli._estimate_batch(disks, states, {"io": {}}, Opts(tmp_path))
    assert total == pytest.approx(8 * 3600 * 3), "old behaviour when no data"


def test_skipping_the_extended_test_removes_it_from_the_estimate(tmp_path,
                                                                 probe_stub):
    disks = [_disk("a")]
    states = {"a": _state(tmp_path, "a", 474)}
    total = cli._estimate_batch(disks, states, {"io": {}},
                                Opts(tmp_path, skip_extended=True))
    assert total == pytest.approx(8 * 3600 * 2)


def test_the_batch_takes_as_long_as_its_slowest_drive(tmp_path, probe_stub):
    """Drives run concurrently, so the total is a max, never a sum."""
    disks = [_disk("a"), _disk("b")]
    states = {"a": _state(tmp_path, "a", 100), "b": _state(tmp_path, "b", 400)}
    total = cli._estimate_batch(disks, states, {"io": {}}, Opts(tmp_path, jobs=2))
    assert total == pytest.approx(8 * 3600 * 2 + 400 * 60)


def test_an_unprobeable_drive_does_not_break_the_estimate(tmp_path, monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def failing(disk, identity, **kw):
        raise OSError("device busy")
        yield

    monkeypatch.setattr(safety, "guarded_open", failing)
    disks = [_disk("a")]
    assert cli._estimate_batch(disks, {"a": _state(tmp_path, "a")},
                               {"io": {}}, Opts(tmp_path)) is None
