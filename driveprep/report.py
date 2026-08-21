"""Report rendering: HTML, PNG, and the batch index (spec 11).

The HTML is self-contained -- inline CSS, no external fetches, no CDN fonts --
because it has to render identically on a machine with no network and survive
being opened years later.

The PNG is the actual deliverable: a listing photo. It is legible as an eBay
thumbnail first and readable at full size second.
"""

from __future__ import annotations

import html
import os
import pwd
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from string import Template

from . import TOOL_NAME, __version__
from . import grade as grading
from . import log

_log = log.get("report")

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

PNG_WIDTH = 1200
PNG_HEIGHT = 1600
MIN_PNG_BYTES = 10 * 1024
RENDER_TIMEOUT_S = 30

# Beyond ROWS_NORMAL attributes the table drops one type size step; beyond
# ROWS_DENSE it truncates with a visible note rather than growing the page
# (spec 11.2).
#
# These are MEASURED against the rendered 1200x1600 page, not estimated. They
# have to be, because the "N further attributes not shown" line states a
# number: if Python emits more rows than the flex region can hold, CSS clips
# the excess and the note undercounts what is actually hidden -- the page then
# contradicts itself in a way a buyer could catch. Emitted rows must always
# fit. Verified capacity is 27 dense / 22 normal; both carry one row of margin.
#
# If the layout above the table changes, re-measure these.
ROWS_NORMAL = 20
ROWS_DENSE = 25

# Grade reasons shown in the badge, one line each, before the "+N more"
# counter. Measured: the badge's reason column holds 4 lines, so 3 reasons
# plus the counter is the most that fits without the counter itself being
# clipped -- which would defeat the point of having it.
BADGE_REASONS = 3

GRADE_GLYPHS = {
    grading.PASS: "✓",       # check
    grading.CAUTION: "!",
    grading.FAIL: "✕",       # ballot X
    grading.INCOMPLETE: "—",  # em dash
}

# Attributes worth highlighting in the table when nonzero.
_NOTABLE = {5, 10, 187, 188, 196, 197, 198, 199}

# Attributes whose raw field is NOT a plain count, so a large value there means
# nothing alarming. Two kinds:
#
#   ratio    Seagate encodes errors-against-total-operations in 1, 7 and 195.
#            "Raw_Read_Error_Rate = 41,452,976" is not 41 million read errors.
#   packed   several counters share the 48-bit field. Attribute 194's low byte
#            is the real temperature; the report showed 64,424,509,467.
#
# Left unannotated on purpose: 241/242 (Total LBAs Written/Read) are genuine
# counts, large but meaningful, and a buyer may well want them.
_RATIO_ENCODED = {1, 7, 195}
_PACKED_RAW = {190, 194, 240}

# A plain reading for these cannot exceed this, so anything above it is packed.
_PACKED_PLAUSIBLE_MAX = {190: 255, 194: 255, 240: 1_000_000}


def _is_vendor_encoded(attr: dict, is_seagate: bool) -> bool:
    """True when this raw is a ratio or packed field rather than a count.

    Value-based rather than purely vendor-based: a drive that reports a
    plausible plain value gets no annotation, so the footnote only appears
    where it is actually needed.
    """
    attr_id = attr.get("id")
    try:
        raw = int(attr.get("raw") or 0)
    except (TypeError, ValueError):
        return False
    if is_seagate and attr_id in _RATIO_ENCODED and raw > 1000:
        return True
    if attr_id in _PACKED_RAW:
        return raw > _PACKED_PLAUSIBLE_MAX.get(attr_id, 0)
    return False

# Why a self-test did not run. The pipeline records these as the status of a
# SelfTestResult with run=False; without the mapping the report would print a
# bare "not run" and discard the reason it already knows.
_NOT_RUN_REASONS = {
    "smart_unavailable": "not run — SMART unavailable through this bridge",
    "skipped": "not run — skipped with --skip-extended-test",
    "not_run": "not run",
}


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _fmt(value, unknown: str = "unknown") -> str:
    if value is None:
        return unknown
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _duration(seconds) -> str:
    if not seconds:
        return "unknown"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------


def _tiles(report: dict) -> str:
    smart = report.get("smart") or {}
    attributes = smart.get("attributes") or []

    def raw(attr_id):
        for attr in attributes:
            if attr.get("id") == attr_id:
                try:
                    return int(attr.get("raw") or 0)
                except (TypeError, ValueError):
                    return None
        return None

    reallocated = raw(5)
    pending = raw(197)
    hours = smart.get("power_on_hours")

    # The tile shows smartctl's DECODED power_on_time.hours; the table below
    # shows attribute 9's raw, which is vendor-encoded and on some firmware
    # counts minutes or seconds, or packs several counters into the 48-bit
    # field (spec 10.1). When the two disagree the page looks self-
    # contradictory to a reader comparing them, so say which is which rather
    # than leaving them to guess who is wrong.
    hours_note = ""
    raw_hours = raw(9)
    if hours is not None and raw_hours is not None and raw_hours != hours:
        hours_note = f"decoded; attribute 9 raw reads {raw_hours:,}"

    # Two different reasons a tile can be empty, and conflating them puts a
    # false statement in front of a buyer: either SMART could not be read at
    # all through the bridge, or SMART is fine and this particular drive simply
    # does not report that attribute (plenty of drives omit 5 or 197).
    missing_note = ("SMART blocked by bridge" if not smart.get("available")
                    else "not reported by this drive")

    tiles = [
        ("Power-on hours", hours, hours_note, ""),
        ("Power cycles", smart.get("power_cycles"), "", ""),
        ("Reallocated sectors", reallocated, "",
         "concern" if reallocated else ""),
        ("Pending sectors", pending, "", "concern" if pending else ""),
    ]

    out = []
    for label, value, note, cls in tiles:
        if value is None:
            value_html = '<div class="tile-value unknown">not available</div>'
            note = note or missing_note
        else:
            value_html = f'<div class="tile-value">{_fmt(value)}</div>'
        note_html = f'<div class="tile-note">{_esc(note)}</div>' if note else ""
        out.append(
            f'<div class="tile {cls}">{value_html}'
            f'<div><div class="tile-label">{_esc(label)}</div>{note_html}</div></div>'
        )
    return "\n".join(out)


def _limitations_note(report: dict) -> str:
    """Framing for a CAUTION whose reasons are all limits of measurement.

    A drive whose USB bridge will not report a self-test has not failed
    anything -- it simply could not be fully measured, and that is a property
    of the enclosure rather than the platters. Rendering that the same as
    "8 reallocated sectors" implies a defect the tool never found, which is
    misleading in the opposite direction from the failure modes elsewhere in
    this file.

    The grade stays CAUTION: less was verified, so the report supports less
    confidence, and that is what the grade means. Only the framing changes.
    """
    grade = report.get("grade") or {}
    if not grade.get("limitations_only"):
        return ""

    drive = report.get("drive") or {}
    bridged = (drive.get("bus_type") or "").lower() == "usb"

    bridge_line = (
        " SMART self-tests are frequently unreadable through a USB enclosure: "
        "the bridge passes the command to the drive but does not return the "
        "result. Connecting this drive directly to SATA would allow a full "
        "self-test."
        if bridged else ""
    )
    return (
        '<section class="limitations-note">'
        "<b>Nothing was found wrong with this drive.</b> Every measurement "
        "that could be taken was within normal range, and the full-surface "
        "erase and verification completed. The notes above record what could "
        "<b>not</b> be measured, not faults."
        f"{bridge_line}"
        "</section>"
    )


def _operator_notes(report: dict) -> str:
    """Disclose events the operator attributed to their own intervention.

    These do not count toward the grade, which is why they must still be
    printed. A report that quietly dropped a recorded kernel event would be
    hiding evidence; one that printed it without its cause would blame the
    drive for a human action. Both are failures of the same kind.
    """
    notes = (report.get("grade") or {}).get("notes") or []
    if not notes:
        return ""
    items = "".join(f"<li>{_esc(n)}</li>" for n in notes)
    return (
        '<section class="operator-notes">'
        "<b>Recorded, but not counted against this drive.</b>"
        f"<ul>{items}</ul>"
        "</section>"
    )


def _incomplete_notice(report: dict) -> str:
    """The 'do not list this' banner for an INCOMPLETE run (spec 10.2).

    Required, and it is the most important line on the page. An incomplete run
    can still have a complete, truthful erase block -- a thermal abort during
    the phase-6 self-test happens after the erase and verify have both
    finished -- so without this banner the page reads as an ordinary
    successful run wearing an odd grey badge, and nothing tells the operator
    it is not fit to publish.
    """
    grade = report.get("grade") or {}
    if grade.get("value") != grading.INCOMPLETE:
        return ""

    reasons = grade.get("reasons") or []
    reason = reasons[0] if reasons else "the run did not finish"
    return (
        '<section class="incomplete-notice">'
        '<div class="not-erased-head">RUN DID NOT COMPLETE '
        '&mdash; DO NOT USE IN A LISTING</div>'
        f'<div class="not-erased-body">{_esc(reason)}<br>'
        "An unfinished run is an incomplete measurement, not a verdict on this "
        "drive. Re-run it to completion before relying on any of the results "
        "below.</div>"
        "</section>"
    )


def _erase_block(report: dict) -> str:
    """Erase + verification block, or the NOT ERASED notice.

    A report that implies a drive was sanitized when it was not is the single
    worst output this tool can produce, so the false case is rendered as
    prominently as the grade badge rather than as a footnote (spec 11.2).
    """
    erase = report.get("erase") or {}
    verify = report.get("verify") or {}

    # A run abandoned at the first failure did read part of the device and
    # did find something. Saying only "not performed" would bury that.
    if verify.get("stopped_on_fail") and (verify.get("read_errors")
                                          or verify.get("nonzero_ranges")):
        errors = verify.get("read_errors") or 0
        nonzero = len(verify.get("nonzero_ranges") or [])
        read = verify.get("bytes_read") or 0
        parts = []
        if errors:
            parts.append(f"{errors} read error(s)"
                         + _first_lba(verify.get("read_error_ranges")))
        if nonzero:
            parts.append(f"{nonzero} region(s) that did not read back as zero")
        return (
            '<section class="block">'
            '<div class="block-title">Erase and verification</div>'
            '<div class="sentence" style="margin-bottom:9px">'
            f"Verification was stopped at the first failure after reading "
            f"{read:,} bytes, and found " + " and ".join(parts) + ". "
            "<b>The rest of the drive was not checked</b>, so the true extent "
            "of the damage is unknown and is likely greater than shown."
            "</div></section>"
        )

    if not erase.get("performed"):
        reason = erase.get("not_performed_reason") or (
            "this drive failed its pre-erase health test"
        )
        return (
            '<section class="not-erased">'
            '<div class="not-erased-head">NOT ERASED</div>'
            f'<div class="not-erased-body">This drive was <b>not written to</b> &mdash; '
            f'{_esc(reason)}. <b>Any previous data is still present.</b> '
            f'No sanitization claim is made for this device.</div>'
            "</section>"
        )

    bytes_written = erase.get("bytes_written") or 0
    verified = verify.get("performed")
    if verified:
        errors = verify.get("read_errors") or 0
        nonzero = len(verify.get("nonzero_ranges") or [])
        if not errors and not nonzero:
            sentence = (
                f"Full-surface verification read confirmed all "
                f"{verify.get('bytes_read', 0):,} bytes read back as zero, "
                f"with 0 read errors."
            )
        else:
            # "covered N bytes and found 7 read errors" invited the reading
            # that all N bytes were verified. They were not: the regions that
            # errored could not be read back at all, so the erase is unproven
            # there. Say so, and say where -- spec 9 records the ranges
            # precisely so the report can state bad regions rather than
            # "failed at 1.2 TB".
            parts = []
            if nonzero:
                parts.append(
                    f"{nonzero} region(s) did not read back as zero"
                    + _first_lba(verify.get("nonzero_ranges"))
                )
            if errors:
                ranges = verify.get("read_error_ranges") or []
                where = f" across {len(ranges)} region(s)" if ranges else ""
                parts.append(
                    f"{errors} read error(s){where}"
                    + _first_lba(ranges)
                )
            sentence = (
                f"Full-surface verification read of "
                f"{verify.get('bytes_read', 0):,} bytes found "
                + "; ".join(parts)
                + ". The erase could not be verified across those regions."
            )
    else:
        sentence = "Full-surface verification read was not completed."

    kv = [
        ("Method", "Single-pass zero overwrite, full device"),
        ("Bytes written", f"{bytes_written:,}"),
        ("Started", erase.get("started_utc") or "unknown"),
        ("Finished", erase.get("finished_utc") or "unknown"),
        ("Erase duration", _duration(erase.get("duration_s"))),
        ("Verify duration", _duration(verify.get("duration_s"))),
        ("Throughput mean", _thr(erase.get("throughput_mean_mbs"))),
        ("Throughput min / max",
         f"{_thr(erase.get('throughput_min_mbs'))} / "
         f"{_thr(erase.get('throughput_max_mbs'))}"),
    ]
    kv_html = "".join(
        f"<div><b>{_esc(k)}</b>{_esc(v)}</div>" for k, v in kv
    )
    return (
        '<section class="block">'
        '<div class="block-title">Erase and verification</div>'
        f'<div class="sentence" style="margin-bottom:9px">{_esc(sentence)}</div>'
        f'<div class="kv">{kv_html}</div>'
        "</section>"
    )


def _thr(value) -> str:
    return f"{value:g} MB/s" if value else "n/a"


def _first_lba(ranges) -> str:
    """", first at LBA N" -- whether the damage is clustered or spread matters.

    LBAs here are byte_offset // logical_block_size, computed by this tool
    (spec 9). They are not the same as lba_of_first_error in the self-test
    blocks, which comes from smartctl and is passed through unmodified.
    """
    if not ranges:
        return ""
    try:
        first = min(int(r["first_lba"]) for r in ranges)
    except (KeyError, TypeError, ValueError):
        return ""
    return f", first at LBA {first:,}"


def _selftest_kv(report: dict) -> str:
    tests = report.get("self_tests") or {}
    out = []
    for key, label in (("short", "Short self-test"),
                       ("extended", "Extended self-test")):
        test = tests.get(key) or {}
        if not test.get("run"):
            # A test that never ran has no duration. Emitting the row anyway
            # rendered "Extended self-test duration: unknown", which reads as
            # "we ran it and lost the number" rather than "we did not run it".
            #
            # Carry the reason through rather than printing a bare "not run":
            # the pipeline already records why, and a buyer asking "so why
            # wasn't it tested?" should not have to infer it from the rest of
            # the page.
            out.append((label, _NOT_RUN_REASONS.get(
                test.get("status") or "", "not run")))
            continue
        out.append((label, (test.get("status") or "unknown").replace("_", " ")))
        if test.get("duration_s"):
            out.append((f"{label} duration", _duration(test.get("duration_s"))))
        elif test.get("rechecked"):
            # The run stopped watching before the drive finished; the outcome
            # was read from the drive's own log afterwards. Say that, rather
            # than printing a bare "unknown" that looks like missing data.
            out.append((f"{label} duration",
                        "not timed \u2014 result read from the drive's log"))
        else:
            out.append((f"{label} duration", _duration(test.get("duration_s"))))
        lba = test.get("lba_of_first_error")
        if lba is not None:
            out.append((f"{label} first error LBA", f"{lba:,}"))
    return "".join(f"<div><b>{_esc(k)}</b>{_esc(v)}</div>" for k, v in out)


def _conditions_kv(report: dict) -> str:
    cond = report.get("run_conditions") or {}
    events = cond.get("kernel_events") or {}
    temp = cond.get("temp_max_c")
    pause = cond.get("thermal_pause_s") or 0

    out = [
        ("Max temperature",
         f"{temp} °C" if temp is not None
         else "not monitored (SMART unavailable)"),
        ("Thermal pauses", _duration(pause) if pause else "none"),
        ("Disconnects", _fmt(cond.get("disconnects") or 0)),
        ("Kernel I/O errors", _fmt(events.get("io_errors") or 0)),
        ("Kernel medium errors", _fmt(events.get("medium_errors") or 0)),
        ("USB resets", _fmt(events.get("usb_resets") or 0)),
        ("UAS / SCSI aborts", _fmt(events.get("uas_aborts") or 0)),
    ]
    if (events.get("usb_resets") or 0) or (events.get("uas_aborts") or 0):
        out.append(("Note", "resets and aborts are usually cable, hub or power"))
    return "".join(f"<div><b>{_esc(k)}</b>{_esc(v)}</div>" for k, v in out)


def _is_notable(attr: dict) -> bool:
    """Does this attribute feed the rubric, or is smartctl flagging it?"""
    return attr.get("id") in _NOTABLE or bool((attr.get("when_failed") or "").strip())


def _select_rows(attributes: list[dict], limit: int) -> list[dict]:
    """Choose which attributes to render when the table must truncate.

    Spec 10 requires that every raw number which fed the grade is printed,
    "so a buyer can form their own judgment". Naive head-truncation breaks
    that: a drive reporting many low-ID attributes could push a flagged one
    past the cut, hiding the exact number that justified its grade.

    So rubric-relevant and smartctl-flagged attributes are retained first, the
    remaining slots are filled in the drive's own order, and the result is
    restored to that order for display -- the table stays a reference table
    rather than a ranked list.
    """
    if len(attributes) <= limit:
        return attributes

    order = {id(attr): index for index, attr in enumerate(attributes)}
    notable = [a for a in attributes if _is_notable(a)]
    ordinary = [a for a in attributes if not _is_notable(a)]

    kept = notable[:limit]
    kept += ordinary[:max(0, limit - len(kept))]
    return sorted(kept, key=lambda a: order[id(a)])


def _smart_rows(report: dict) -> tuple[str, str, str]:
    """(rows_html, density_class, truncation_note_html)."""
    smart = report.get("smart") or {}
    if not smart.get("available"):
        row = (
            '<tr><td class="l" colspan="8" style="padding:14px 7px;'
            'font-size:16px">SMART data is not available through this '
            "drive's USB bridge. Connecting the drive directly to SATA would "
            "yield full SMART data.</td></tr>"
        )
        return row, "", ""

    attributes = smart.get("attributes") or []
    model = ((report.get("drive") or {}).get("model") or "").upper()
    is_seagate = model.startswith("ST") or "SEAGATE" in model

    density = "" if len(attributes) <= ROWS_NORMAL else "dense"
    limit = ROWS_NORMAL if not density else ROWS_DENSE
    shown = _select_rows(attributes, limit)
    hidden = len(attributes) - len(shown)

    rows = []
    annotated = False
    for attr in shown:
        try:
            raw = int(attr.get("raw") or 0)
        except (TypeError, ValueError):
            raw = 0
        when = (attr.get("when_failed") or "").strip()
        flagged = bool(when) or (_is_notable(attr) and raw > 0)
        encoded = _is_vendor_encoded(attr, is_seagate)
        if encoded:
            annotated = True
        raw_cell = f"{raw:,}&#8202;†" if encoded else f"{raw:,}"
        rows.append(
            f'<tr class="{"flagged" if flagged else ""}">'
            f'<td class="l">{_esc(attr.get("id"))}</td>'
            f'<td class="l">{_esc(attr.get("name"))}</td>'
            f'<td>{_esc(attr.get("value"))}</td>'
            f'<td>{_esc(attr.get("worst"))}</td>'
            f'<td>{_esc(attr.get("thresh"))}</td>'
            f"<td>{raw_cell}</td>"
            f'<td class="l">{_esc(attr.get("flags"))}</td>'
            f'<td class="l">{_esc(when or "-")}</td>'
            "</tr>"
        )

    note = ""
    if hidden > 0:
        note = (
            f'<div class="truncated">{hidden} further attribute(s) not shown '
            f"&mdash; see report.json for the complete set.</div>"
        )
    if annotated:
        note += (
            '<div class="attr-note">&#8202;† Vendor-encoded raw &mdash; a ratio '
            "or several packed counters, not a plain count. A large number here "
            "is normal and does not indicate errors. None of these affect the "
            "grade.</div>"
        )
    return "\n".join(rows), density, note


def _methodology(report: dict) -> str:
    drive = report.get("drive") or {}
    erase = report.get("erase") or {}
    parts = []

    if erase.get("performed"):
        verify = report.get("verify") or {}
        unverified = bool(verify.get("read_errors")
                          or verify.get("nonzero_ranges"))
        # Do not claim "every sector was read back to confirm the erase
        # completed" when the read-back errored or found live data -- the
        # sentence in the erase block directly above says the opposite, and a
        # methodology footer that contradicts its own report is worse than no
        # footer at all.
        readback = (
            "then a read-back of every sector was attempted; the regions noted "
            "above could not be confirmed"
            if unverified else
            "then every sector was read back to confirm the erase completed"
        )
        parts.append(
            "The entire device was overwritten with a single pass of zeros from "
            "LBA 0 to the last block, including the partition table and all "
            f"slack, {readback}. For modern magnetic recording a single "
            "overwrite pass renders data unrecoverable by any known practical "
            "technique; this corresponds to the &ldquo;Clear&rdquo; level of "
            "NIST SP 800-88 Rev. 1 for magnetic media."
        )
    else:
        # "below" was wrong: the methodology footer sits underneath the health
        # sections it was referring to.
        parts.append(
            "No erase was performed on this drive, so no sanitization claim is "
            "made. The health results shown above were still captured."
        )

    parts.append(
        "ATA Secure Erase was not used: most USB bridges do not pass ATA "
        "security commands through, and on SATA the drive is normally frozen by "
        "the BIOS at boot, which makes it a poor fit for an unattended batch."
    )

    enclosure = (drive.get("enclosure") or "") + " " + (drive.get("model") or "")
    if "my book" in enclosure.lower():
        parts.append(
            "This is a WD My Book class unit, whose USB bridge implements "
            "hardware AES. Zeros written through such a bridge are stored on the "
            "platters as the ciphertext of zeros rather than as literal zeros. "
            "This is still a complete and correct sanitization of the user data, "
            "and the verification read through the same bridge is the meaningful "
            "verification."
        )

    if report.get("flags", {}).get("skipped_extended_test"):
        parts.append("The SMART extended self-test was skipped for this run.")

    parts.append(
        "<b>This is seller-generated documentation, not a third-party "
        "certification and not a certificate of destruction.</b>"
    )
    return " ".join(parts)


def _css() -> str:
    """The shared stylesheet, inlined.

    Kept in one file rather than duplicated between report.html and the print
    bundle: two copies would drift, and the printed pages must match the
    listing image exactly. Still inlined at render time, so every artifact
    stays self-contained with no external fetches.
    """
    return (TEMPLATE_DIR / "report.css").read_text(encoding="utf-8")


def _report_sheet(report: dict) -> str:
    """The report page markup, with every placeholder filled but no <style>."""
    template = Template((TEMPLATE_DIR / "report.html").read_text(encoding="utf-8"))
    body = template.template.split("</style>", 1)[1]
    return Template(body).safe_substitute(_report_fields(report))


def _instructions_sheet(report: dict) -> str:
    """Page two: how the buyer initialises and looks after the drive."""
    template = Template(
        (TEMPLATE_DIR / "buyer-instructions.html").read_text(encoding="utf-8"))
    drive = report.get("drive") or {}

    form_bits = [drive.get("form_factor") or "",
                 (drive.get("bus_type") or "").upper()]
    if drive.get("rotation_rpm"):
        form_bits.insert(1, f"{drive['rotation_rpm']:,} RPM")

    seller = report.get("seller_name") or ""
    return template.safe_substitute({
        "model_short": _esc(drive.get("model") or "Hard disk drive"),
        "capacity_label": _esc(drive.get("capacity_label") or ""),
        "form_factor_line": _esc("  ·  ".join(b for b in form_bits if b)),
        "seller_line": (f"Sold by {_esc(seller)}." if seller else ""),
        "tool_line": _esc(
            f"{TOOL_NAME} {report.get('tool', {}).get('version', __version__)}"),
        "report_id": _esc(report.get("report_id", "")),
    })


def render_print_bundle(report: dict, path: Path) -> Path:
    """Two-page printable document: the test report, then buyer instructions.

    Separate from report.html because the two have different jobs. report.png
    is a listing photo and must be a single image; this is what goes in the box
    with the drive.
    """
    template = Template(
        (TEMPLATE_DIR / "print-bundle.html").read_text(encoding="utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template.safe_substitute({
        "css": _css(),
        "report_id": _esc(report.get("report_id", "")),
        "page_one": _report_sheet(report),
        "page_two": _instructions_sheet(report),
    }), encoding="utf-8")
    _log.debug("wrote %s", path)
    return path


def _report_fields(report: dict) -> dict:
    """Every placeholder the report sheet needs.

    Shared by render_html() and the print bundle so the listing image and the
    printed page can never disagree about what the drive measured.
    """
    drive = report.get("drive") or {}
    grade = report.get("grade") or {}
    value = grade.get("value", grading.INCOMPLETE)

    reasons = grade.get("reasons") or []
    if not reasons and value == grading.PASS:
        reasons = ["No FAIL or CAUTION condition was met."]

    # The badge shows at most BADGE_REASONS one-line entries and then an
    # explicit counter, so a drive with more reasons than fit never looks like
    # it had only the ones displayed. Every reason is in report.json, and the
    # numbers behind them are all in the SMART table below regardless.
    shown = reasons[:BADGE_REASONS]
    reasons_html = "".join(f"<li>{_esc(r)}</li>" for r in shown)
    remaining = len(reasons) - len(shown)
    if remaining > 0:
        reasons_html += (
            f'<li class="more">+{remaining} more reason'
            f'{"s" if remaining != 1 else ""} &mdash; see report.json</li>'
        )

    rows, density, truncated = _smart_rows(report)

    form_bits = [drive.get("form_factor") or "", drive.get("bus_type", "").upper()]
    if drive.get("rotation_rpm"):
        form_bits.insert(1, f"{drive['rotation_rpm']:,} RPM")
    form_line = "  ·  ".join(b for b in form_bits if b)

    serial_line = drive.get("ata_serial") or drive.get("enclosure_serial") or ""

    payload = {
        "report_id": _esc(report.get("report_id", "")),
        "vendor": _esc(drive.get("vendor") or "Hard disk drive"),
        "model": _esc(drive.get("model") or "Unknown model"),
        "capacity_label": _esc(drive.get("capacity_label") or ""),
        "form_factor_line": _esc(form_line),
        # An all-limitations CAUTION is styled as information, not alarm.
        "grade_class": (value.lower() + " informational"
                        if (grade.get("limitations_only") and
                            value == grading.CAUTION) else value.lower()),
        "grade_word": _esc(value),
        "grade_glyph": ("i" if (grade.get("limitations_only") and
                                value == grading.CAUTION)
                        else GRADE_GLYPHS.get(value, "?")),
        "grade_reasons": reasons_html,
        "incomplete_notice": _incomplete_notice(report)
                             + _limitations_note(report)
                             + _operator_notes(report),
        "tiles": _tiles(report),
        "erase_block": _erase_block(report),
        "selftest_kv": _selftest_kv(report),
        "conditions_kv": _conditions_kv(report),
        "smart_rows": rows,
        "smart_density": density,
        "smart_truncated": truncated,
        "methodology": _methodology(report),
        "tool_line": _esc(
            f"{TOOL_NAME} {report.get('tool', {}).get('version', __version__)}"
            f"  ·  serial {serial_line}"
        ),
        "seller": _esc(report.get("seller_name") or ""),
        "generated": _esc(report.get("generated_utc", "")),
    }

    return payload


def render_html(report: dict, path: Path) -> Path:
    """Render report.html. Self-contained: no external fetches of any kind."""
    template = Template((TEMPLATE_DIR / "report.html").read_text(encoding="utf-8"))
    payload = _report_fields(report)
    payload["css"] = _css()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template.safe_substitute(payload), encoding="utf-8")
    _log.debug("wrote %s", path)
    return path


# --------------------------------------------------------------------------
# PNG
# --------------------------------------------------------------------------


CHROME_CANDIDATES = [
    "/opt/google/chrome/chrome",       # recommended: real deb, no confinement
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
]


def find_chrome() -> tuple[str, bool] | tuple[None, bool]:
    """(binary path, is_snap). Resolution order per spec 11.3."""
    for candidate in CHROME_CANDIDATES:
        path = candidate if os.path.isabs(candidate) else shutil.which(candidate)
        if not path or not os.path.exists(path):
            continue
        real = os.path.realpath(path)
        is_snap = real.startswith("/snap/") or "/snap/" in real
        return path, is_snap
    return None, False


def _invoking_user_home() -> Path | None:
    """The home of the user who ran sudo -- NOT $HOME.

    This tool always runs as root, where $HOME is /root, which is outside snap
    confinement just as /var/lib is. Using it would fix nothing.
    """
    name = os.environ.get("SUDO_USER")
    if name:
        try:
            return Path(pwd.getpwnam(name).pw_dir)
        except KeyError:
            pass
    uid = os.environ.get("SUDO_UID")
    if uid:
        try:
            return Path(pwd.getpwuid(int(uid)).pw_dir)
        except (KeyError, ValueError):
            pass
    return None


def png_dimensions(path: Path) -> tuple[int, int] | None:
    """Read width/height from the IHDR chunk. No image library needed."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def render_png(html_path: Path, png_path: Path) -> bool:
    """Render the HTML to a fixed-size PNG with headless Chrome.

    Never fails a 20-hour run over a missing browser: if no Chromium is
    available the HTML is left in place with a clear message and the caller
    continues.
    """
    binary, is_snap = find_chrome()
    if not binary:
        _log.warning(
            "no Chrome or Chromium found, so %s was not rendered. The HTML "
            "report is complete at %s -- open it in any browser and screenshot "
            "it at 1200x1600. To render automatically, install Google Chrome "
            "(see README).", png_path.name, html_path,
        )
        return False

    source_html, target_png = html_path, png_path
    staging: Path | None = None

    if is_snap:
        home = _invoking_user_home()
        if home is None or not home.is_dir():
            _log.warning(
                "the only browser found is the Chromium snap (%s), whose "
                "confinement cannot read %s, and there is no SUDO_USER home to "
                "stage through. Skipping PNG; the HTML report is complete.",
                binary, html_path,
            )
            return False
        staging = Path(tempfile.mkdtemp(prefix="driveprep-render-", dir=home))
        source_html = staging / "report.html"
        target_png = staging / "report.png"
        shutil.copy2(html_path, source_html)
        _chown_to_invoker(staging)

    profile = tempfile.mkdtemp(prefix="driveprep-chrome-")
    cmd = [
        binary,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",             # required when running as root
        "--hide-scrollbars",
        "--no-first-run",
        f"--user-data-dir={profile}",  # avoids the /root profile singleton lock
        f"--screenshot={target_png}",
        f"--window-size={PNG_WIDTH},{PNG_HEIGHT}",
        "--default-background-color=FFFFFFFF",
        f"file://{source_html}",
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=RENDER_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        _log.error("PNG render timed out after %d s", RENDER_TIMEOUT_S)
        _cleanup(profile, staging)
        return False
    except OSError as exc:
        _log.error("could not run %s: %s", binary, exc)
        _cleanup(profile, staging)
        return False

    if staging is not None and target_png.exists():
        shutil.copy2(target_png, png_path)

    ok = _validate_png(png_path, proc)
    _cleanup(profile, staging)
    return ok


def _validate_png(png_path: Path, proc) -> bool:
    """A zero-byte or missing PNG is a miserable thing to debug silently."""
    if not png_path.exists():
        _log.error(
            "PNG was not produced. Chrome said: %s",
            (proc.stderr or proc.stdout or "(nothing)").strip()[:400],
        )
        return False
    size = png_path.stat().st_size
    if size < MIN_PNG_BYTES:
        _log.error(
            "PNG is only %d bytes, under the %d byte sanity floor -- treating "
            "as a failed render. Chrome said: %s",
            size, MIN_PNG_BYTES,
            (proc.stderr or proc.stdout or "(nothing)").strip()[:400],
        )
        return False
    dims = png_dimensions(png_path)
    if dims and dims != (PNG_WIDTH, PNG_HEIGHT):
        _log.warning("PNG is %dx%d, expected %dx%d", *dims, PNG_WIDTH, PNG_HEIGHT)
    _log.info("rendered %s (%d bytes, %sx%s)", png_path.name, size,
              *(dims or ("?", "?")))
    return True


def render_pdf(html_path: Path, pdf_path: Path) -> bool:
    """Render a print bundle to PDF via headless Chrome.

    Same resolution and snap handling as the PNG path, and the same rule: a
    missing browser prints a message and continues rather than failing a run.
    """
    binary, is_snap = find_chrome()
    if not binary:
        _log.warning(
            "no Chrome or Chromium found, so %s was not rendered. The "
            "printable HTML is complete at %s -- open it and print from there.",
            pdf_path.name, html_path,
        )
        return False

    source, target = html_path, pdf_path
    staging: Path | None = None
    if is_snap:
        home = _invoking_user_home()
        if home is None or not home.is_dir():
            _log.warning("snap Chromium cannot read %s and there is no "
                         "SUDO_USER home to stage through; skipping PDF",
                         html_path)
            return False
        staging = Path(tempfile.mkdtemp(prefix="driveprep-pdf-", dir=home))
        source, target = staging / "bundle.html", staging / "bundle.pdf"
        shutil.copy2(html_path, source)
        _chown_to_invoker(staging)

    profile = tempfile.mkdtemp(prefix="driveprep-chrome-")
    cmd = [
        binary, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--no-first-run", f"--user-data-dir={profile}",
        "--no-pdf-header-footer", f"--print-to-pdf={target}",
        f"file://{source}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=RENDER_TIMEOUT_S, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log.error("PDF render failed: %s", exc)
        _cleanup(profile, staging)
        return False

    if staging is not None and target.exists():
        shutil.copy2(target, pdf_path)

    ok = pdf_path.exists() and pdf_path.stat().st_size > 4096
    if not ok:
        _log.error("PDF was not produced. Chrome said: %s",
                   (proc.stderr or proc.stdout or "(nothing)").strip()[:400])
    else:
        _log.info("rendered %s (%d bytes, %d page(s))", pdf_path.name,
                  pdf_path.stat().st_size, pdf_page_count(pdf_path))
    _cleanup(profile, staging)
    return ok


def pdf_page_count(path: Path) -> int:
    """Page count from the PDF's page-tree /Count. No PDF library needed."""
    try:
        blob = path.read_bytes()
    except OSError:
        return 0
    counts = re.findall(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", blob, re.S)
    if counts:
        return max(int(c) for c in counts)
    return len(re.findall(rb"/Type\s*/Page\b", blob))


def _chown_to_invoker(path: Path) -> None:
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if not uid:
        return
    try:
        for child in [path, *path.rglob("*")]:
            os.chown(child, int(uid), int(gid or uid))
    except (OSError, ValueError) as exc:
        _log.debug("could not chown staging dir: %s", exc)


def _cleanup(profile: str, staging: Path | None) -> None:
    shutil.rmtree(profile, ignore_errors=True)
    if staging:
        shutil.rmtree(staging, ignore_errors=True)


# --------------------------------------------------------------------------
# Batch index
# --------------------------------------------------------------------------


_BATCH_CSS = """
body{font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
background:#fff;color:#0b0b0b;margin:0;padding:32px 40px;
font-variant-numeric:tabular-nums}
h1{font-size:26px;margin:0 0 4px}
.sub{color:#52514e;font-size:14px;margin-bottom:22px}
table{border-collapse:collapse;width:100%}
th{text-align:left;font-size:12px;letter-spacing:.07em;text-transform:uppercase;
color:#52514e;border-bottom:2px solid #b4b3ae;padding:6px 10px}
td{padding:7px 10px;border-bottom:1px solid #d8d8d5;font-size:14px}
.g{font-weight:800;padding:2px 10px;border:2px solid #b4b3ae;display:inline-block}
.g.pass{border-color:#0ca30c;background:#ddf2dd}
.g.caution{border-color:#fab219;background:#fef4df}
.g.fail{border-color:#d03b3b;background:#f8e4e4}
.g.incomplete{border-color:#57606a;background:#e7e9ea}
a{color:#0b0b0b}
"""


def render_batch_index(batch: dict, directory: Path) -> None:
    """index.html + index.json summarizing every drive in a batch."""
    directory.mkdir(parents=True, exist_ok=True)

    rows = []
    for entry in batch.get("drives", []):
        value = entry.get("grade", grading.INCOMPLETE)
        glyph = GRADE_GLYPHS.get(value, "?")
        reason = (entry.get("reasons") or [""])[0]
        rows.append(
            "<tr>"
            f'<td><a href="../../{_esc(entry.get("output_name", ""))}/report.html">'
            f'{_esc(entry.get("drive_id", ""))}</a></td>'
            f'<td>{_esc(entry.get("model", ""))}</td>'
            f'<td>{_esc(entry.get("capacity_label", ""))}</td>'
            f'<td><span class="g {value.lower()}">{glyph} {_esc(value)}</span></td>'
            f'<td>{_esc(reason)}</td>'
            "</tr>"
        )

    html_doc = (
        f"<meta charset='utf-8'><title>DrivePrep batch "
        f"{_esc(batch.get('batch_id', ''))}</title>"
        f"<style>{_BATCH_CSS}</style>"
        f"<h1>DrivePrep batch {_esc(batch.get('batch_id', ''))}</h1>"
        f"<div class='sub'>{len(batch.get('drives', []))} drive(s) &middot; "
        f"generated {_esc(batch.get('generated_utc', ''))}</div>"
        "<table><thead><tr><th>Drive</th><th>Model</th><th>Capacity</th>"
        "<th>Grade</th><th>Leading reason</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table>"
    )
    (directory / "index.html").write_text(html_doc, encoding="utf-8")

    from .state import atomic_write_json
    atomic_write_json(directory / "index.json", batch)
    _log.info("wrote batch index to %s", directory)
