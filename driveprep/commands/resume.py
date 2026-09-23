"""`resume`: pick interrupted drives back up, through the same gate as `run`."""

from __future__ import annotations

from pathlib import Path

from .. import grade as grading, inventory as inv, safety, state as st
from . import batch


def cmd_resume(options) -> int:
    """A resumed run writes to devices, so it goes through the identical gate.

    The token is recomputed from the drives it is about to resume. It will
    differ from the original batch's token whenever the resumed set is smaller,
    which is correct: a drive that finished, was pulled, or was replaced must
    not be silently swept back into a destructive run.
    """
    config = grading.load_config()
    output_root = Path(options.output_root)
    resumable = st.find_resumable(output_root)
    if not resumable:
        print("Nothing to resume.")
        return 0

    disks = inv.scan()
    batch.evaluate_all(disks, options)

    from .. import pipeline as pipe

    selected: list[inv.Disk] = []
    states: dict[str, st.DriveState] = {}
    contested: set[str] = set()
    for directory in resumable:
        drive_state = st.DriveState.load(directory)
        if drive_state is None:
            continue
        match, refusal = pipe.match_stored_drive(disks, drive_state)
        if refusal:
            print(f"  REFUSED: {drive_state.drive_id}: {refusal}")
            continue
        if match is None:
            print(f"  skipping {drive_state.drive_id}: its identity tuple no "
                  f"longer matches any attached device")
            continue
        if match.id in states or match.id in contested:
            if match.id not in contested:
                print(f"  REFUSED: {match.id} matches more than one stored "
                      f"run; refusing to guess which one it belongs to")
                del states[match.id]
                selected[:] = [d for d in selected if d.id != match.id]
                contested.add(match.id)
            continue
        if not match.eligible:
            print(f"  skipping {match.id}: no longer eligible -- "
                  f"{match.ineligible_reasons[0]}")
            continue
        selected.append(match)
        drive_state.output_dir = directory
        states[match.id] = drive_state

    # The same per-drive lock `run` holds for the whole pipeline. Without it,
    # resume could pick up a drive a live run is still erasing -- its state is
    # below the report phase, so it looks resumable -- and overwrite that run's
    # checkpoint.
    return batch.run_locked(
        selected, states,
        lambda: _resume_batch(selected, states, config, options, output_root))


def _resume_batch(selected, states, config, options, output_root) -> int:
    if not selected:
        print("No resumable drive is still attached and eligible.")
        return 0

    token = safety.compute_token(selected)
    manifest = safety.Manifest(
        disks=selected, token=token,
        total_bytes=sum(d.size_bytes for d in selected),
        estimate_seconds=None,
    )
    print(f"\nResuming {len(selected)} drive(s). The token is recomputed from "
          f"the resumed set, so it will differ from the original batch's token "
          f"if that set is smaller.")
    return batch.confirm_and_run(manifest, selected, states, config, options,
                                 output_root)

