"""One drive's bad SMART data must not end the pre-confirmation phases."""
from driveprep import pipeline as pipe, state as st
from driveprep.commands import run as cli


class Opts:
    execute = False
    chunk_size = None


class Disk:
    def __init__(self, id_):
        self.id = id_
        self.logical_block_bytes = 512
        self.physical_block_bytes = 512
        self.short_test_status = None
        self.skip_erase = False


def test_a_bad_drive_does_not_stop_the_others(monkeypatch, tmp_path):
    seen = []

    def phase1(self):
        seen.append(self.disk.id)
        if self.disk.id == "bad":
            raise ValueError("malformed SMART JSON")

    def gate(self):
        if self.disk.id == "bad":
            raise KeyError("ata_smart_attributes")
        return []

    monkeypatch.setattr(pipe.DrivePipeline, "phase1_smart_before", phase1)
    monkeypatch.setattr(pipe.DrivePipeline, "smart_gate", gate)
    disks = [Disk("bad"), Disk("good")]
    states = {name: st.DriveState(drive_id=name, output_dir=tmp_path / name,
                                  batch_id="B", run_id="R")
              for name in ("bad", "good")}

    cli._run_pre_gate_phases(disks, states, {"io": {}, "checkpoint": {}},
                             Opts())

    assert seen == ["bad", "good"]
    assert disks[1].short_test_status == "not run (plan mode)"
    assert states["bad"].smart_available is False, \
        "a failed snapshot must not leave SMART marked available"
