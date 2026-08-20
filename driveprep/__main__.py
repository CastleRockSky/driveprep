"""driveprep CLI (spec 8).

    sudo driveprep list                              # inventory, never writes
    sudo driveprep run --all                         # plan: manifest + token
    sudo driveprep run --all --execute               # execute a batch
    sudo driveprep run --id <by-id> --execute
    sudo driveprep run --all --execute --confirm-token DP-4-A7F3 --jobs 4
    sudo driveprep resume --execute                  # re-confirms, see spec 4.4
    sudo driveprep report --all                      # rebuild reports only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from . import blockio, grade as grading, inventory as inv
from . import log, report as reporting, safety
from . import state as st, supervisor as sup

_log = log.get("cli")

DEFAULT_OUTPUT_ROOT = "/var/lib/driveprep"


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="driveprep",
        description="Securely erase, health-test, and document used hard disk "
                    "drives for resale.",
        epilog="This tool destroys data irreversibly. Nothing is written "
               "without --execute and a matching confirmation token.",
    )
    parser.add_argument("--version", action="version",
                        version=f"driveprep {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT,
                       help=f"default {DEFAULT_OUTPUT_ROOT}")
        p.add_argument("--seller-name", default="",
                       help="optional line on the report")
        p.add_argument("--mask-serial", action="store_true",
                       help="redact the middle of serials in the report")

    p_list = sub.add_parser("list", help="inventory every attached disk")
    add_common(p_list)

    p_run = sub.add_parser("run", help="plan or execute a batch")
    add_common(p_run)
    p_run.add_argument("--id", action="append", default=[], dest="ids",
                       metavar="BY_ID", help="repeatable; primary selector")
    p_run.add_argument("--device", action="append", default=[], dest="devices",
                       metavar="/dev/sdX",
                       help="escape hatch for drives with no by-id entry")
    p_run.add_argument("--all", action="store_true", dest="all_drives",
                       help="every eligible drive")
    p_run.add_argument("--test-mode", action="store_true",
                       help="permit loop and dm devices only; requires --device")
    p_run.add_argument("--execute", action="store_true",
                       help="required for any write")
    p_run.add_argument("--confirm-token", default=None,
                       help="pre-answer the confirmation; still recomputed")
    p_run.add_argument("--jobs", type=int, default=4, help="concurrency")
    p_run.add_argument("--chunk-size", type=int, default=None,
                       help="transfer chunk in bytes, default 8 MiB")
    p_run.add_argument("--usb-only", action="store_true",
                       help="convenience gate; refuse non-USB targets")
    p_run.add_argument("--skip-extended-test", action="store_true",
                       help="cuts phase 6; report is marked and grades CAUTION")
    p_run.add_argument("--stop-on-fail", action="store_true",
                       help="abandon a drive at the first FAIL condition "
                            "instead of mapping every bad sector. Off by "
                            "default: a truncated run records less evidence")

    p_resume = sub.add_parser("resume", help="resume interrupted drives")
    add_common(p_resume)
    p_resume.add_argument("--execute", action="store_true")
    p_resume.add_argument("--confirm-token", default=None)
    p_resume.add_argument("--jobs", type=int, default=4)
    p_resume.add_argument("--chunk-size", type=int, default=None)
    p_resume.add_argument("--test-mode", action="store_true")
    p_resume.add_argument("--skip-extended-test", action="store_true")
    p_resume.add_argument("--stop-on-fail", action="store_true")
    p_resume.add_argument("--usb-only", action="store_true")

    p_print = sub.add_parser("print", help="print the two-page report bundle")
    add_common(p_print)
    p_print.add_argument("--id", action="append", default=[], dest="ids",
                         help="repeatable; defaults to every completed drive")
    p_print.add_argument("--all", action="store_true", dest="all_drives")
    p_print.add_argument("--printer", default=None,
                         help="CUPS destination; defaults to the system default")
    p_print.add_argument("--copies", type=int, default=1)
    p_print.add_argument("--dry-run", action="store_true",
                         help="render the PDF but do not send it to a printer")

    p_recheck = sub.add_parser(
        "recheck", help="re-read a self-test result the drive finished later")
    add_common(p_recheck)
    p_recheck.add_argument("--id", action="append", default=[], dest="ids")
    p_recheck.add_argument("--all", action="store_true", dest="all_drives")

    p_report = sub.add_parser("report", help="rebuild reports from stored state")
    add_common(p_report)
    p_report.add_argument("--all", action="store_true", dest="all_drives")
    p_report.add_argument("--id", action="append", default=[], dest="ids")

    return parser


def _normalize(args) -> argparse.Namespace:
    """Fill in defaults the subcommand did not declare, so options is uniform."""
    for name, default in (
        ("ids", []), ("devices", []), ("all_drives", False),
        ("test_mode", False), ("execute", False), ("confirm_token", None),
        ("jobs", 4), ("chunk_size", None), ("usb_only", False),
        ("skip_extended_test", False), ("stop_on_fail", False),
        ("seller_name", ""),
        ("mask_serial", False), ("verbose", False),
    ):
        if not hasattr(args, name):
            setattr(args, name, default)
    args.batch_id = f"B-{log.utcstamp()[:10].replace('-', '')}-" \
                    f"{log.utcstamp()[11:16].replace(':', '')}"
    args.scan_hint = {}
    return args


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def _select(disks: list[inv.Disk], options) -> list[inv.Disk]:
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


def _evaluate_all(disks: list[inv.Disk], options) -> None:
    protected = safety.protected_disks(Path(options.output_root))
    for disk in disks:
        safety.evaluate(
            disk, test_mode=options.test_mode, usb_only=options.usb_only,
            output_root=Path(options.output_root), protected=protected,
        )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_list(options) -> int:
    disks = inv.scan()
    _evaluate_all(disks, options)

    print(f"\n{len(disks)} block device(s) found\n")
    for disk in disks:
        mark = "ELIGIBLE" if disk.eligible else "INELIGIBLE"
        tag = " [INTERNAL BUS]" if disk.is_internal_bus and disk.eligible else ""
        print(f"  {disk.id}")
        print(f"    {mark}{tag}  {disk.kname}  {disk.capacity_label}  "
              f"{disk.bus_type}  {disk.model or '(no model)'}")
        if disk.sysfs_rotational is not None:
            print(f"    sysfs rotational: {disk.sysfs_rotational} (advisory only)")
        for reason in disk.ineligible_reasons:
            print(f"      - {reason}")
        print()
    return 0


def cmd_run(options) -> int:
    config = grading.load_config()
    disks = inv.scan()
    _evaluate_all(disks, options)
    selected = _select(disks, options)

    refused = [d for d in selected if not d.eligible]
    if refused:
        print("\nREFUSED -- these targets are ineligible:\n")
        for disk in refused:
            print(f"  {disk.id}")
            for reason in disk.ineligible_reasons:
                print(f"    - {reason}")
        print()
        return 2

    if not selected:
        print("No eligible drives selected. Nothing to do.")
        return 0

    output_root = Path(options.output_root)
    states = _prepare_states(selected, options, output_root)

    # Per-drive locks, held for the WHOLE pipeline (spec 8.1). O_EXCL only
    # covers phases 4-5; phases 0-3 hold no descriptor at all, so without this
    # a second instance can plan the same drive, pass the gate, and overwrite
    # this run's checkpoint -- destroying accumulated findings -- before its own
    # O_EXCL open finally fails. Acquired in the parent and inherited by the
    # forked children, which is what keeps them held to phase 8.
    locks = _acquire_locks(selected, states)
    if not selected:
        print("Every selected drive is locked by another DrivePrep instance.")
        return 2
    try:
        return _run_batch(selected, states, config, options, output_root)
    finally:
        for lock in locks:
            lock.release()


def _acquire_locks(selected, states):
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


def _run_batch(selected, states, config, options, output_root) -> int:
    # Phase 0 is complete. The token is computed HERE, over the eligible set
    # that we actually hold locks for, and before any self-test runs (spec
    # 4.4). Computing it before locking would print a token covering a drive
    # this run is not going to touch.
    token = safety.compute_token(selected)

    # Phases 1 and 2 run HERE, before the gate (spec 7): the operator confirms
    # once with full information, including short-test results, then leaves.
    # Running them inside the child instead would confirm first and test after,
    # which defeats the point of phase 2 being a gate the operator sees.
    _run_pre_gate_phases(selected, states, config, options)

    estimate = _estimate_batch(selected, states, config, options)
    manifest = safety.Manifest(
        disks=selected, token=token,
        total_bytes=sum(d.size_bytes for d in selected),
        estimate_seconds=estimate,
    )

    if not safety.confirm(manifest, execute=options.execute,
                          supplied_token=options.confirm_token):
        return 0 if not options.execute else 2

    work = [(disk, states[disk.id], False) for disk in selected]
    supervisor = sup.Supervisor(config, options)
    summary = supervisor.run(work)
    directory = sup.write_batch_index(summary, output_root)

    _print_summary(summary, directory)
    return _exit_code(summary)


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
    _evaluate_all(disks, options)

    selected: list[inv.Disk] = []
    states: dict[str, st.DriveState] = {}
    for directory in resumable:
        drive_state = st.DriveState.load(directory)
        if drive_state is None:
            continue
        match = next((d for d in disks if d.identity == drive_state.identity), None)
        if match is None:
            print(f"  skipping {drive_state.drive_id}: its identity tuple no "
                  f"longer matches any attached device")
            continue
        if not match.eligible:
            print(f"  skipping {match.id}: no longer eligible -- "
                  f"{match.ineligible_reasons[0]}")
            continue
        selected.append(match)
        drive_state.output_dir = directory
        states[match.id] = drive_state

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
    if not safety.confirm(manifest, execute=options.execute,
                          supplied_token=options.confirm_token):
        return 0 if not options.execute else 2

    work = [(disk, states[disk.id], False) for disk in selected]
    supervisor = sup.Supervisor(config, options)
    summary = supervisor.run(work)
    directory = sup.write_batch_index(summary, output_root)
    _print_summary(summary, directory)
    return _exit_code(summary)


def cmd_report(options) -> int:
    """Rebuild reports from stored state. Never touches a device."""
    import json
    output_root = Path(options.output_root)
    if not output_root.is_dir():
        print(f"No output root at {output_root}")
        return 1

    rebuilt = 0
    for directory in sorted(output_root.iterdir()):
        if directory.name == "batches" or not directory.is_dir():
            continue
        if options.ids and directory.name not in options.ids:
            continue
        path = directory / "report.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  {directory.name}: cannot read report.json ({exc})")
            continue
        data["grade"] = grading.evaluate(data, grading.load_config()).to_json()
        st.atomic_write_json(path, data)
        html_path = reporting.render_html(data, directory / "report.html")
        reporting.render_png(html_path, directory / "report.png")
        bundle = reporting.render_print_bundle(
            data, directory / "report-print.html")
        reporting.render_pdf(bundle, directory / "report.pdf")
        print(f"  {directory.name}: {data['grade']['value']}")
        rebuilt += 1

    print(f"\nRebuilt {rebuilt} report(s). No device was opened.")
    return 0


def cmd_print(options) -> int:
    """Print the two-page bundle: test report, then buyer setup instructions.

    Never touches a device. Renders from stored state, so it can be run long
    after the drive has been packed.
    """
    import json
    import subprocess

    output_root = Path(options.output_root)
    if not output_root.is_dir():
        print(f"No output root at {output_root}")
        return 1

    targets = []
    for directory in sorted(output_root.iterdir()):
        if directory.name == "batches" or not directory.is_dir():
            continue
        if options.ids and directory.name not in options.ids:
            continue
        if (directory / "report.json").exists():
            targets.append(directory)

    if not targets:
        print("Nothing to print. Run `driveprep report --all` first, or check "
              "--output-root.")
        return 1

    printer = options.printer or _default_printer()
    if printer is None and not options.dry_run:
        print(
            "No printer configured, and none given with --printer.\n"
            "  See what CUPS knows about:   lpstat -p -d\n"
            "  Add a network printer:       lpadmin -p NAME -E -v "
            "ipp://<address>/ipp/print -m everywhere\n"
            "The PDFs are still written; use --dry-run to render without "
            "printing."
        )

    failures = 0
    for directory in targets:
        try:
            data = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  {directory.name}: cannot read report.json ({exc})")
            failures += 1
            continue

        bundle = reporting.render_print_bundle(
            data, directory / "report-print.html")
        pdf = directory / "report.pdf"
        if not reporting.render_pdf(bundle, pdf):
            print(f"  {directory.name}: could not render a PDF; "
                  f"open {bundle} and print it from a browser")
            failures += 1
            continue

        pages = reporting.pdf_page_count(pdf)
        grade = data.get("grade", {}).get("value", "?")
        if grade == grading.INCOMPLETE:
            # The report itself says DO NOT USE IN A LISTING; do not quietly
            # hand the operator a printed copy to put in a box.
            print(f"  {directory.name}: SKIPPED -- graded INCOMPLETE. "
                  f"This run did not finish and must not ship with a drive.")
            failures += 1
            continue

        if options.dry_run or printer is None:
            print(f"  {directory.name}: {grade}, {pages} page(s) -> {pdf}")
            continue

        cmd = ["lp", "-d", printer, "-n", str(options.copies), str(pdf)]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            print(f"  {directory.name}: lp failed -- {proc.stderr.strip()[:200]}")
            failures += 1
        else:
            print(f"  {directory.name}: {grade}, {pages} page(s) -> "
                  f"{printer} ({proc.stdout.strip()})")

    return 1 if failures else 0


def cmd_recheck(options) -> int:
    """Re-read a self-test the drive finished after the run gave up on it.

    An extended self-test can outlive the tool's patience: if the polling
    deadline is reached the result is recorded as `inconclusive`, but the drive
    keeps going and writes the real outcome to its own self-test log. This
    reads that log and corrects the record.

    Constrained deliberately, because this rewrites a health result:

      * it only ever reads the drive's own log -- no value is inferred
      * it only replaces a result that was inconclusive or interrupted; a
        recorded pass or failure is never overwritten
      * it will not touch a drive that has since been re-erased or is mid-run
      * a failing outcome is written exactly as read, so this can only ever
        make a report worse as readily as better

    Never opens a device for writing.
    """
    import json

    from . import smart

    output_root = Path(options.output_root)
    if not output_root.is_dir():
        print(f"No output root at {output_root}")
        return 1

    disks = {d.id: d for d in inv.scan()}
    changed = 0

    for directory in sorted(output_root.iterdir()):
        if directory.name == "batches" or not directory.is_dir():
            continue
        if options.ids and directory.name not in options.ids:
            continue
        report_path = directory / "report.json"
        state_path = directory / "state.json"
        if not report_path.exists():
            continue

        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  {directory.name}: unreadable report.json ({exc})")
            continue

        recorded = (report.get("self_tests") or {}).get("extended") or {}
        status = (recorded.get("status") or "").lower()
        # "skipped" is the ABSENCE of a result, not a verdict -- an operator who
        # passed --skip-extended-test and later ran the test by hand should get
        # the real outcome. A recorded pass or failure is still never touched.
        if status not in ("inconclusive", "interrupted", "skipped", "not_run"):
            print(f"  {directory.name}: extended test is {status!r}; "
                  f"nothing to recheck")
            continue

        drive_id = report.get("drive", {}).get("by_id") or directory.name
        disk = disks.get(drive_id)
        if disk is None:
            print(f"  {directory.name}: not currently attached; skipping")
            continue

        d_type = report.get("drive", {}).get("smartctl_device_type")
        entry = smart.last_selftest_entry(disk.dev_path, d_type)
        if not entry:
            print(f"  {directory.name}: the drive reports no self-test log")
            continue

        kind = ((entry.get("type") or {}).get("string") or "").lower()
        if "extended" not in kind:
            print(f"  {directory.name}: newest log entry is {kind!r}, not an "
                  f"extended test; leaving the record alone")
            continue

        # The entry must be NEWER than the run, or an old self-test from
        # before the erase could be promoted into this report -- claiming the
        # drive passed a surface test it only passed in a previous life. The
        # drive's own lifetime hours are the clock; the report records the
        # hours at its after-snapshot. Strictly greater, because a test
        # finishing within the same hour could fall either side of it.
        lifetime = entry.get("lifetime_hours")
        run_hours = (report.get("smart") or {}).get("power_on_hours")
        if lifetime is None or run_hours is None:
            print(f"  {directory.name}: cannot date the self-test against the "
                  f"run; refusing to promote it")
            continue
        if int(lifetime) <= int(run_hours):
            print(f"  {directory.name}: newest extended test is from "
                  f"{lifetime}h but the run ended at {run_hours}h -- it "
                  f"predates this erase, so it says nothing about it")
            continue

        status_obj = entry.get("status") or {}
        passed = status_obj.get("passed")
        raw_status = (status_obj.get("string") or "").strip().lower()

        if passed is None:
            # An extended test that was attempted and did not conclude --
            # aborted, interrupted, still running. Not a verdict about the
            # drive, but it IS a result worth recording: "inconclusive" tells a
            # reader the test was tried, where "skipped" invites them to think
            # nobody bothered. Read from the drive's log, never assumed.
            if any(k in raw_status for k in
                   ("abort", "interrupt", "in progress", "fatal", "unknown")):
                result = {
                    "run": True, "status": "inconclusive", "duration_s": None,
                    "lba_of_first_error": None, "rechecked": True,
                    "log_status": status_obj.get("string"),
                }
                report.setdefault("self_tests", {})["extended"] = result
                report["grade"] = grading.evaluate(
                    report, grading.load_config()).to_json()
                st.atomic_write_json(report_path, report)
                if state_path.exists():
                    try:
                        sd = json.loads(state_path.read_text(encoding="utf-8"))
                        sd["extended_test"] = result
                        st.atomic_write_json(state_path, sd)
                    except (OSError, json.JSONDecodeError):
                        pass
                html = reporting.render_html(report, directory / "report.html")
                reporting.render_png(html, directory / "report.png")
                bundle = reporting.render_print_bundle(
                    report, directory / "report-print.html")
                reporting.render_pdf(bundle, directory / "report.pdf")
                print(f"  {directory.name}: extended test -> inconclusive "
                      f"({status_obj.get('string')})  "
                      f"(grade now {report['grade']['value']})")
                changed += 1
                continue
            print(f"  {directory.name}: log entry has no pass/fail; skipping")
            continue

        lba = entry.get("lba") if passed is False else None
        result = {
            "run": True,
            "status": ("completed_without_error" if passed
                       else (raw_status or "unknown").replace(" ", "_")),
            # Deliberately dropped. The recorded duration measures how long the
            # RUN watched the test, not how long the DRIVE took -- for a test
            # abandoned after 20 minutes and finished hours later, printing
            # "20 min" on a buyer-facing page is simply false. The drive's log
            # gives the outcome but not the elapsed time, so the honest answer
            # is that it was not measured.
            "duration_s": None,
            "lba_of_first_error": lba,
            "rechecked": True,
        }

        report.setdefault("self_tests", {})["extended"] = result
        report["grade"] = grading.evaluate(report, grading.load_config()).to_json()
        st.atomic_write_json(report_path, report)

        if state_path.exists():
            try:
                state_data = json.loads(state_path.read_text(encoding="utf-8"))
                state_data["extended_test"] = result
                st.atomic_write_json(state_path, state_data)
            except (OSError, json.JSONDecodeError):
                pass

        html = reporting.render_html(report, directory / "report.html")
        reporting.render_png(html, directory / "report.png")
        bundle = reporting.render_print_bundle(
            report, directory / "report-print.html")
        reporting.render_pdf(bundle, directory / "report.pdf")

        print(f"  {directory.name}: extended test -> {result['status']}  "
              f"(grade now {report['grade']['value']})")
        changed += 1

    print(f"\nRechecked {changed} report(s). No device was opened for writing.")
    return 0


def _default_printer() -> str | None:
    import subprocess
    try:
        proc = subprocess.run(["lpstat", "-d"], capture_output=True, text=True,
                              timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or "").strip()
    if ":" in text and "no system default" not in text.lower():
        return text.split(":", 1)[1].strip()
    return None


# --------------------------------------------------------------------------
# Support
# --------------------------------------------------------------------------


def _prepare_states(disks, options, output_root: Path) -> dict:
    states = {}
    started = log.utcstamp()
    for disk in disks:
        directory = output_root / disk.output_name
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


def _run_pre_gate_phases(disks, states, config, options) -> None:
    """Phase 1 for every drive, and phase 2 as well under --execute (spec 7).

    Plan mode deliberately stops after phase 1: the short self-test takes
    minutes per drive and perturbs the drive's self-test log, and since the
    token comes from the phase-0 set it has no bearing on what plan mode shows
    (spec 4.4).
    """
    from . import pipeline as pipe

    for disk in disks:
        pipeline = pipe.DrivePipeline(disk, states[disk.id], config, options)
        try:
            pipeline.phase1_smart_before()
        except Exception as exc:  # noqa: BLE001 - never lose the batch to one drive
            _log.warning("%s: SMART snapshot failed (%s); continuing",
                         disk.id, exc)

        # A drive already failing on SMART is skipped before the short test:
        # no point spending two minutes, let alone eight hours, on it.
        failures = pipeline.smart_gate()
        if failures:
            disk.short_test_status = "SKIPPED -- SMART already failing"
            disk.skip_erase = True
            continue

        if not options.execute:
            disk.short_test_status = "not run (plan mode)"
            continue

        try:
            if pipeline.phase2_short_test():
                status = (states[disk.id].short_test or {}).get("status")
                disk.short_test_status = (status or "not run").replace("_", " ")
            else:
                disk.short_test_status = "FAILED -- this drive will be SKIPPED"
                disk.skip_erase = True
        except Exception as exc:  # noqa: BLE001
            _log.warning("%s: short self-test failed to run (%s); continuing",
                         disk.id, exc)
            disk.short_test_status = "could not run"


def _estimate_batch(disks, states, config, options) -> float | None:
    """Read-only duration estimate (spec 7).

    Derived from a 1 GB sequential READ from the middle of each device.
    Nothing is written: phase 3 is the confirmation gate, so any calibration
    write before it would put bytes on the platters -- including LBA 0 --
    before the operator has confirmed.

    Two corrections learned from a real 2 x 4 TB batch that was estimated at
    21.7 h and took 28.6 h:

      * The probes run CONCURRENTLY, matching --jobs. Run one at a time they
        measure uncontended throughput, but the actual passes share a bus and a
        bridge; two 4 TB verifies came in at 12.6 h each against an 8 h
        uncontended prediction. Measuring under contention beats applying a
        made-up factor to a clean number.
      * The extended self-test is taken from the drive's OWN polling estimate
        rather than assumed to cost another full pass. That figure is known
        exactly, and on those drives it was 7.9 h against a 12.6 h pass.

    Still approximate, and knowingly so: the probe reads, while phase 4 writes,
    and on these drives writing ran faster than reading. Using the read rate
    for both makes the erase leg pessimistic, which is the safer direction for
    a number an operator plans their day around.
    """
    from concurrent.futures import ThreadPoolExecutor

    def probe(disk):
        """Returns (disk, seconds-per-full-pass) or (disk, None)."""
        cfg = blockio.PassConfig(
            chunk_bytes=options.chunk_size
            or config.get("io", {}).get("chunk_bytes", blockio.DEFAULT_CHUNK),
            logical_block_bytes=disk.logical_block_bytes,
            physical_block_bytes=disk.physical_block_bytes,
        )
        try:
            with safety.guarded_open(
                disk, disk.identity, write=False,
                test_mode=options.test_mode,
                output_root=Path(options.output_root),
            ) as (fd, _kname):
                return disk, blockio.estimate_duration(fd, disk.size_bytes, cfg)
        except (safety.SafetyError, OSError) as exc:
            _log.debug("could not estimate %s: %s", disk.id, exc)
            return disk, None

    workers = max(1, min(getattr(options, "jobs", 1), len(disks)))

    # Measure BOTH ends rather than pick one and pretend to precision.
    # Uncontended is what a lone drive achieves; contended is what N drives
    # sharing a bus achieve. The real run sits between them, because phase 4
    # writes (which contend less here) and phase 5 reads (which contend more).
    # A single number was 16% out whichever endpoint it used; a range that
    # actually contains the answer is more use to someone planning a day.
    # Keyed by disk.id, not the Disk itself: Disk is a dataclass, so it
    # generates __eq__ and is therefore unhashable.
    solo = {d.id: v for d, v in (probe(d) for d in disks)}
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            together = {d.id: v for d, v in pool.map(probe, disks)}
    else:
        together = solo

    low = high = 0.0
    for disk in disks:
        fast, slow = solo.get(disk.id), together.get(disk.id)
        if not fast or not slow:
            continue
        extended = (0.0 if options.skip_extended_test
                    else _extended_test_seconds(states.get(disk.id), slow))
        # Drives run concurrently, so the batch takes as long as its slowest.
        low = max(low, fast * 2 + extended)
        high = max(high, slow * 2 + extended)
    if not high:
        return None
    return (low, high) if high > low * 1.05 else high


def _extended_test_seconds(state, one_pass: float) -> float:
    """The drive's own estimate for phase 6, falling back to a pass-equivalent."""
    if state is not None and state.smart_before_data:
        from . import smart
        minutes = smart.SmartResult(
            available=True, d_type=state.smartctl_d_type,
            data=state.smart_before_data).selftest_polling_minutes.get("extended")
        if minutes:
            return minutes * 60
    return one_pass


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


COMMANDS = {
    "list": cmd_list,
    "recheck": cmd_recheck,
    "print": cmd_print,
    "run": cmd_run,
    "resume": cmd_resume,
    "report": cmd_report,
}


def main(argv: list[str] | None = None) -> int:
    args = _normalize(build_parser().parse_args(argv))
    log.setup(verbose=args.verbose)
    log.require_root()
    try:
        return COMMANDS[args.command](args)
    except safety.SafetyError as exc:
        _log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 3


if __name__ == "__main__":
    sys.exit(main())
