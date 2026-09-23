"""`report`, `print` and `recheck`: commands that work from stored output.

None of these opens a device for writing. `recheck` reads a self-test log from
an attached drive; the others work from report.json and state.json alone.
"""

from __future__ import annotations

from pathlib import Path

from .. import grade as grading, inventory as inv, log, report as reporting
from .. import runs, state as st

_log = log.get("cli")


def _rescan_kernel_log(directory: Path, data: dict) -> None:
    """Re-derive kernel-log evidence for a finished run from its stored epochs.

    The counts in a report are only as good as the scan that produced them, and
    two defects made every count structurally zero: 'SuperSpeed' failed to match
    the reset pattern, and UTC epoch stamps were handed to journalctl as local
    wall time. Reports written before those fixes carry zeros that were never
    measurements. This re-runs the sweep against the same epochs and replaces
    them.

    Bounded by journal retention: if the journal no longer reaches back to the
    run, the sweep returns nothing and the report keeps its existing counts
    rather than gaining a fresh set of zeros that would look like evidence.
    """
    import json

    from .. import identity as ident, kernlog

    state_path = directory / "state.json"
    if not state_path.exists():
        print(f"  {directory.name}: no state.json; kernel log not rescanned")
        return
    try:
        stored = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  {directory.name}: cannot read state.json ({exc})")
        return

    history = ident.LocatorHistory.from_json(stored.get("locator_epochs"))
    if not history.epochs:
        print(f"  {directory.name}: no locator epochs recorded; skipped")
        return

    run_start = (stored.get("run_started_utc")
                 or history.epochs[0].valid_from)
    events = kernlog.sweep(history, run_start)

    conditions = data.setdefault("run_conditions", {})
    before = conditions.get("kernel_events") or {}
    after = dict(events.counts)

    if not events.lines:
        # "No faults found" and "the evidence is gone" both produce an empty
        # sweep, and they mean opposite things. Saying "retention?" for a run
        # the journal still fully covers would understate a genuinely clean
        # result; saying "clean" for a run rotated out of the journal would
        # invent evidence. Decide by whether the journal still reaches the run.
        if kernlog.journal_covers(run_start):
            conditions["kernel_events"] = after
            print(f"  {directory.name}: kernel log rescanned -- no faults "
                  f"found (journal still covers this run)")
        else:
            print(f"  {directory.name}: journal no longer reaches this run; "
                  f"counts left as recorded, not re-verified")
        return

    conditions["kernel_events"] = after
    kernlog.write_log_file(events, directory / "kernel-log.txt")
    changed = {k: (before.get(k, 0), v) for k, v in after.items()
               if before.get(k, 0) != v}
    if changed:
        detail = ", ".join(f"{k} {o}->{n}" for k, (o, n) in changed.items())
        print(f"  {directory.name}: kernel log rescanned -- {detail}")


def cmd_report(options) -> int:
    """Rebuild reports from stored state. Never touches a device."""
    import json
    output_root = Path(options.output_root)
    if not output_root.is_dir():
        print(f"No output root at {output_root}")
        return 1

    rebuilt = 0
    wanted = runs.requested_ids(options)
    targets, matched, ambiguous = runs.select_runs(wanted, output_root)
    if ambiguous:
        runs.report_ambiguous(ambiguous)
        return 1
    for directory in targets:
        path = directory / "report.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  {directory.name}: cannot read report.json ({exc})")
            continue
        if getattr(options, "rescan_kernel_log", False):
            _rescan_kernel_log(directory, data)

        excused = getattr(options, "operator_disconnects", None)
        if excused:
            if not options.ids:
                print("  --operator-disconnect requires --id: it is a claim "
                      "about one run, not a blanket exemption.")
                return 1
            count = int(excused[-1])
            recorded = ((data.get("run_conditions") or {})
                        .get("kernel_events") or {}).get("usb_resets") or 0
            if count > recorded:
                print(f"  {directory.name}: refusing to excuse {count} "
                      f"disconnect(s); only {recorded} were recorded.")
                return 1
            attributed = (data.setdefault("run_conditions", {})
                              .setdefault("operator_attributed_events", {}))
            attributed["usb_resets"] = count
            print(f"  {directory.name}: {count} of {recorded} disconnect(s) "
                  f"attributed to operator action")

        data["grade"] = grading.evaluate(data, grading.load_config()).to_json()
        st.atomic_write_json(path, data)
        html_path = reporting.render_html(data, directory / "report.html")
        reporting.render_png(html_path, directory / "report.png")
        bundle = reporting.render_print_bundle(
            data, directory / "report-print.html")
        reporting.render_pdf(bundle, directory / "report.pdf")
        print(f"  {directory.name}: {data['grade']['value']}")
        rebuilt += 1

    runs.report_unmatched(wanted, matched, output_root)
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

    wanted = runs.requested_ids(options)
    resolved, matched, ambiguous = runs.select_runs(wanted, output_root)
    if ambiguous:
        runs.report_ambiguous(ambiguous)
        return 1
    targets = [d for d in resolved if (d / "report.json").exists()]

    runs.report_unmatched(wanted, matched, output_root)
    if not targets:
        if not wanted:
            print("Nothing to print. Run `driveprep report --all` first, or "
                  "check --output-root.")
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

    from .. import smart

    output_root = Path(options.output_root)
    if not output_root.is_dir():
        print(f"No output root at {output_root}")
        return 1

    disks = {d.id: d for d in inv.scan()}
    changed = 0
    wanted = runs.requested_ids(options)
    targets, matched, ambiguous = runs.select_runs(wanted, output_root)
    if ambiguous:
        runs.report_ambiguous(ambiguous)
        return 1

    for directory in targets:
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

        # state.json keeps the real by-id name; report.json's may be masked
        # by --mask-serial and would then match no attached drive.
        drive_id = (runs.stored_by_id(state_path)
                    or report.get("drive", {}).get("by_id") or directory.name)
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

    runs.report_unmatched(wanted, matched, output_root)
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

