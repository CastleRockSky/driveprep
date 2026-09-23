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

from . import __version__, log, runs, safety
from .commands import documents, resume, run

_log = log.get("cli")


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
        p.add_argument("--output-root", default=runs.DEFAULT_OUTPUT_ROOT,
                       help=f"default {runs.DEFAULT_OUTPUT_ROOT}")
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
                         metavar="BY_ID",
                         help="repeatable; defaults to every completed drive. "
                              "Takes the by-id name or the output directory "
                              "name; either spelling matches")
    p_print.add_argument("--all", action="store_true", dest="all_drives")
    p_print.add_argument("--printer", default=None,
                         help="CUPS destination; defaults to the system default")
    p_print.add_argument("--copies", type=int, default=1)
    p_print.add_argument("--dry-run", action="store_true",
                         help="render the PDF but do not send it to a printer")

    p_recheck = sub.add_parser(
        "recheck", help="re-read a self-test result the drive finished later")
    add_common(p_recheck)
    p_recheck.add_argument("--id", action="append", default=[], dest="ids",
                           metavar="BY_ID",
                           help="repeatable; by-id or output directory name")
    p_recheck.add_argument("--all", action="store_true", dest="all_drives")

    p_report = sub.add_parser("report", help="rebuild reports from stored state")
    add_common(p_report)
    p_report.add_argument("--all", action="store_true", dest="all_drives")
    p_report.add_argument("--id", action="append", default=[], dest="ids",
                          metavar="BY_ID",
                          help="repeatable; by-id or output directory name")
    p_report.add_argument(
        "--operator-disconnect", action="append", default=[], metavar="N",
        dest="operator_disconnects",
        help="attribute N recorded USB reset/disconnect events to your own "
             "physical intervention (unplugging the drive, swapping a cable). "
             "They stay in the report and in kernel-log.txt; they are simply "
             "not counted against the drive. Requires --id, because this is a "
             "claim about one specific run.")
    p_report.add_argument(
        "--rescan-kernel-log", action="store_true", dest="rescan_kernel_log",
        help="re-derive kernel-log evidence from the stored locator epochs "
             "instead of reusing the counts recorded during the run. Needed "
             "for reports written before a scanning fix, and bounded by how "
             "far back the journal still reaches.")

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


COMMANDS = {
    "list": run.cmd_list,
    "recheck": documents.cmd_recheck,
    "print": documents.cmd_print,
    "run": run.cmd_run,
    "resume": resume.cmd_resume,
    "report": documents.cmd_report,
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

