"""What `run` and `resume` share: selection, eligibility, locks, and the gate.

Both write to devices, so both must pass through the identical confirmation
gate (spec 4.4) and hold the per-drive lock for the whole pipeline (spec 8.1).
Keeping one copy of that sequence here is what stops the two from drifting:
`resume` once went without the lock because its copy of the tail did not have
it.
"""

from __future__ import annotations

from pathlib import Path

from .. import grade as grading, inventory as inv, safety, supervisor as sup


def select_targets(disks: list[inv.Disk], options) -> list[inv.Disk]:
    """Resolve the requested targets. Refuses the unsafe combinations."""
    if options.test_mode and options.all_drives:
        raise SystemExit(
            "REFUSED: --test-mode may not be combined with --all.\n"
            "A stock Ubuntu Server box keeps 15-25 snap loop devices mounted, "
            "and --all would sweep the whole set into a batch. Every one of "
            "them is refused correctly, but the result is a screen of refusals "
            "that teaches you to ignore them. Name test targets explicitly "
            "with --device /dev/loopN."
        )

    if options.all_drives:
        return [d for d in disks if d.eligible]

    wanted = [*options.ids, *options.devices]
    if not wanted:
        raise SystemExit(
            "Nothing selected. Use --all, or --id <by-id name>, or "
            "--device /dev/sdX. Run `driveprep list` to see what is attached."
        )

    selected = []
    for name in wanted:
        disk = inv.find_by_id(disks, name)
        if disk is None:
            raise SystemExit(f"REFUSED: {name!r} does not match any attached disk.")
        selected.append(disk)

    if options.devices and not options.test_mode and options.jobs > 1 \
            and len(selected) > 1:
        raise SystemExit(
            "REFUSED: --device drives have no by-id entry and cannot be safely "
            "re-identified after a replug, so they may not be used in queue "
            "mode. Run them one at a time with --jobs 1, or use --id."
        )
    return selected


def evaluate_all(disks: list[inv.Disk], options) -> None:
    protected = safety.protected_disks(Path(options.output_root))
    for disk in disks:
        safety.evaluate(
            disk, test_mode=options.test_mode, usb_only=options.usb_only,
            output_root=Path(options.output_root), protected=protected,
        )


def acquire_locks(selected, states):
    """Lock every drive we are about to touch; drop any already held."""
    locks = []
    for disk in list(selected):
        lock = sup.DriveLock(states[disk.id].lock_path)
        if lock.acquire():
            locks.append(lock)
            continue
        print(f"  REFUSED: {disk.id} is locked by another DrivePrep instance "
              f"(see {states[disk.id].lock_path}). Skipping it.")
        selected.remove(disk)
    return locks


def _print_summary(summary: dict, directory: Path) -> None:
    print("\n" + "=" * 78)
    print("  BATCH COMPLETE")
    print("=" * 78)
    for entry in summary["drives"]:
        print(f"  {entry['grade']:<11} {entry['drive_id']}")
        for reason in entry["reasons"][:2]:
            print(f"              {reason}")
    print(f"\n  Batch index: {directory / 'index.html'}\n")


def _exit_code(summary: dict) -> int:
    values = {d["grade"] for d in summary["drives"]}
    if grading.FAIL in values or grading.INCOMPLETE in values:
        return 1
    return 0


def run_locked(selected, states, body) -> int:
    """Hold every selected drive's lock around body(); release on the way out.

    Drives another instance already holds are dropped from `selected` before
    body() runs, and the token is computed inside it, over the set actually
    locked.
    """
    locks = acquire_locks(selected, states)
    try:
        return body()
    finally:
        for lock in locks:
            lock.release()


def confirm_and_run(manifest, selected, states, config, options,
                    output_root: Path) -> int:
    """The gate, then the batch: confirm, supervise, index, summarize."""
    if not safety.confirm(manifest, execute=options.execute,
                          supplied_token=options.confirm_token):
        return 0 if not options.execute else 2

    work = [(disk, states[disk.id], False) for disk in selected]
    supervisor = sup.Supervisor(config, options)
    summary = supervisor.run(work)
    directory = sup.write_batch_index(summary, output_root)
    _print_summary(summary, directory)
    return _exit_code(summary)
