"""Stored runs: finding them on disk, and naming new ones.

Every per-drive command that works from output rather than from attached
hardware (`report`, `print`, `recheck`) selects runs here, and `run` uses the
same rules to decide where a drive's output belongs.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import inventory as inv, log, state as st

_log = log.get("cli")

DEFAULT_OUTPUT_ROOT = "/var/lib/driveprep"


class OutputCollision(RuntimeError):
    """A drive's output directory already belongs to a different drive."""


def requested_ids(options) -> set[str]:
    """The --id values, normalised so either spelling of an identifier hits.

    `driveprep list` prints by-id names, and on USB those carry colons
    (``...-0:0``). The output directory spells the same drive with
    underscores. Pasting what list printed used to match nothing.
    """
    return {inv.sanitize_id(value) for value in getattr(options, "ids", [])}


def stored_aliases(directory: Path) -> set[str]:
    """Every identifier that should resolve to this stored run.

    The directory name alone is not enough. Since each physical drive got its
    own output directory, a dock drive's directory is `<bay>__<serial>` -- so
    matching the directory name exactly means `--id <bay-name>`, which is what
    `driveprep list` prints and what the operator pastes, resolves only to the
    OLD pre-serial directory belonging to whichever drive ran in that bay
    before. It printed a CAUTION report for one drive when asked for another
    that had graded FAIL.

    So a request also matches the by-id name and the serials recorded INSIDE
    the run. Ambiguity that creates is refused by the caller, never guessed.
    """
    aliases = {directory.name}
    for name in ("report.json", "state.json"):
        path = directory / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        drive = data.get("drive") or {}
        for key in ("by_id", "ata_serial", "enclosure_serial", "output_name"):
            value = drive.get(key) or data.get(key)
            if isinstance(value, str) and value.strip():
                aliases.add(inv.sanitize_id(value.strip()))
        break
    return aliases


def _stored_runs(output_root: Path) -> list[Path]:
    """Every directory under the output root that holds a run."""
    return [d for d in sorted(output_root.iterdir())
            if d.is_dir() and d.name != "batches"]


def select_runs(wanted: set[str], output_root: Path):
    """Map each --id onto the one stored run it names.

    Returns (directories, matched_requests, ambiguous_messages). An --id that
    names more than one stored run is REFUSED rather than resolved to an
    arbitrary one: the whole failure this replaces was a command quietly
    acting on a drive the operator did not ask for.
    """
    candidates = _stored_runs(output_root)
    if not wanted:
        return candidates, set(), []

    selected: dict[str, Path] = {}
    matched: set[str] = set()
    ambiguous: list[str] = []
    for request in sorted(wanted):
        # An exact directory name deliberately does NOT win outright. The bay
        # name IS the exact name of the pre-serial directory, so letting it win
        # would reproduce the original bug in the one case that caused it: the
        # operator pastes what `list` printed and silently gets whichever drive
        # ran in that bay first. When several runs answer to one identifier the
        # answer is "say which", not "pick one".
        hits = [d for d in candidates if request in stored_aliases(d)]
        if not hits:
            continue
        if len(hits) > 1:
            lines = "".join(f"      {_run_label(d)}\n" for d in sorted(hits))
            ambiguous.append(f"  {request}\n{lines}")
            continue
        matched.add(request)
        selected[hits[0].name] = hits[0]
    return [selected[k] for k in sorted(selected)], matched, ambiguous


def _run_label(directory: Path) -> str:
    """Directory name plus the serial it recorded, so a refusal is actionable.

    The pre-serial directory's own name is the ambiguous identifier, so naming
    directories alone would send the operator round in a circle. The serial
    always resolves to exactly one run.
    """
    serial = None
    for name in ("report.json", "state.json"):
        path = directory / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            break
        drive = data.get("drive") or {}
        serial = drive.get("ata_serial") or drive.get("enclosure_serial")
        break
    return f"{directory.name}" + (f"   (serial {serial})" if serial else "")


def report_ambiguous(ambiguous: list[str]) -> None:
    """Name the --id values that answered to more than one stored run."""
    if not ambiguous:
        return
    print("This identifier names more than one stored run, so it is refused "
          "rather than guessed at.\nPass the full directory name, or the "
          "serial, of the one you mean:")
    for block in ambiguous:
        print(block, end="")


def report_unmatched(wanted: set[str], matched: set[str],
                      output_root: Path) -> None:
    """Name the --id values that answered to no stored run.

    Saying nothing was the bug worth fixing: an --id with a typo produced the
    same output as an empty output root, so the advice was to rebuild reports
    that already existed.
    """
    missing = sorted(wanted - matched)
    if not missing:
        return
    print("No stored run matches:")
    for name in missing:
        print(f"  {name}")
    available = sorted(
        d.name for d in output_root.iterdir()
        if d.is_dir() and d.name != "batches" and (d / "report.json").exists()
    )
    if available:
        print("Stored runs are:")
        for name in available:
            print(f"  {name}")


def stored_by_id(state_path: Path) -> str | None:
    import json
    try:
        return json.loads(state_path.read_text(encoding="utf-8")).get("by_id")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def stored_serial(directory: Path) -> str | None:
    """Which physical drive a stored run belongs to, or None if unrecorded."""
    import json
    for path, pick in (
            (directory / "state.json", lambda d: d.get("enclosure_serial")),
            (directory / "report.json", lambda d: (d.get("drive") or {}).get(
                "ata_serial") or (d.get("drive") or {}).get("enclosure_serial")),
    ):
        try:
            value = pick(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if value:
            return str(value).strip()
    return None


def output_dir_for(output_root: Path, disk) -> tuple[Path, str | None]:
    """Where this drive's run belongs, and any reason it must not go there.

    Two jobs. First, adopt the pre-serial directory when it holds THIS drive's
    own history, so renaming the scheme does not strand a drive's past runs in
    a directory nothing points at any more.

    Second, refuse a directory that belongs to a different drive. With the
    serial in the name that should be unreachable, but a drive whose serial
    cannot be read still falls back to the bay name -- and silently reusing
    another drive's directory is the failure this whole change exists to
    prevent, so it is worth saying out loud rather than assuming away.
    """
    directory = output_root / disk.output_name
    serial = (disk.serial or "").strip()

    legacy = output_root / inv.sanitize_id(disk.id)
    if legacy != directory and not directory.exists() and legacy.is_dir():
        if serial and stored_serial(legacy) == serial:
            directory = legacy

    stored = stored_serial(directory)
    if stored and stored != serial:
        # An UNKNOWN serial is refused too, deliberately. It cannot prove the
        # directory is its own, and the choice is between refusing a re-run
        # and overwriting another drive's finished report on a guess.
        whose = f"this drive is {serial}" if serial else (
            "this drive's serial cannot be read, so it cannot be shown to be "
            "the same one")
        return directory, (
            f"{disk.id}: {directory} already holds a run for drive {stored}, "
            f"but {whose}. The identifier names the bay, not the drive. Move "
            f"that directory aside if you want this run to take its place -- "
            f"it will not be overwritten."
        )
    return directory, None


def prepare_states(disks, options, output_root: Path) -> dict:
    states = {}
    started = log.utcstamp()
    for disk in disks:
        directory, problem = output_dir_for(output_root, disk)
        if problem:
            raise OutputCollision(problem)
        directory.mkdir(parents=True, exist_ok=True)
        drive_state = st.DriveState(
            drive_id=disk.id,
            output_dir=directory,
            batch_id=options.batch_id,
            run_id=f"R-{started}",
            identity=disk.identity,
            by_id=disk.by_id,
            kernel_name=disk.kname,
            model=disk.model,
            enclosure_serial=disk.serial,
            capacity_bytes=disk.size_bytes,
            logical_block_bytes=disk.logical_block_bytes,
            physical_block_bytes=disk.physical_block_bytes,
            run_started_utc=started,
        )
        drive_state.checkpoint(force=True)
        states[disk.id] = drive_state
    return states

