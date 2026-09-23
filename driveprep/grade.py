"""Grading rubric (spec 10).

Deterministic, published in the report itself, and driven from
config/grading.toml so thresholds can be tuned without editing code.

The grade is computed, never operator-supplied. There is no override flag.

Which number the rubric reads matters more than anything else here. Every SMART
attribute has a normalized `value` (typically starting at 100 or 200 and
counting DOWN toward `thresh`) and a vendor-specific `raw`. Getting this
backwards silently breaks the entire rubric: reading attribute 5's normalized
value would compare a healthy drive's 200 against "> 0" and grade every drive
CAUTION.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import log
from .smart import SELFTEST_FAILED, SELFTEST_PASSED, selftest_verdict

_log = log.get("grade")

PASS = "PASS"
CAUTION = "CAUTION"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"

VALID_GRADES = (PASS, CAUTION, FAIL, INCOMPLETE)

_DEFAULT_CONFIG = Path(__file__).resolve().parent / "config" / "grading.toml"


def load_config(path: Path | None = None) -> dict:
    path = path or _DEFAULT_CONFIG
    with open(path, "rb") as handle:
        return tomllib.load(handle)


@dataclass
class Grade:
    value: str
    reasons: list[str] = field(default_factory=list)
    rubric_version: int = 1
    # Reasons that are limits of measurement rather than findings about the
    # drive: a bridge that blocks SMART, a self-test the enclosure will not
    # report, a test the operator skipped. They still grade CAUTION -- the
    # grade reflects how much confidence the report supports -- but they are a
    # different claim from "this drive has 8 reallocated sectors", and a report
    # that renders them identically implies a defect that was never found.
    limitations: list[str] = field(default_factory=list)
    # Statements of fact that are neither findings nor limits: something the
    # report must disclose but that does not bear on the grade. An event the
    # operator caused belongs here -- suppressing it would hide a real kernel
    # event, and grading on it would blame the drive for a human action.
    notes: list[str] = field(default_factory=list)

    @property
    def limitations_only(self) -> bool:
        """True when nothing was found wrong; things simply could not be seen."""
        return (self.value == CAUTION
                and bool(self.limitations)
                and len(self.limitations) == len(self.reasons))

    def to_json(self) -> dict:
        return {
            "value": self.value,
            "reasons": self.reasons,
            "rubric_version": self.rubric_version,
            "limitations": self.limitations,
            "limitations_only": self.limitations_only,
            "notes": self.notes,
        }


def _attr(attributes: list[dict], attr_id: int) -> dict | None:
    for attr in attributes:
        if attr.get("id") == attr_id:
            return attr
    return None


def _raw(attributes: list[dict], attr_id: int) -> int | None:
    attr = _attr(attributes, attr_id)
    if attr is None:
        return None
    try:
        return int(attr.get("raw") or 0)
    except (TypeError, ValueError):
        return None


# run=False statuses graded by some other rule: a skip by
# skipped_extended_test, missing SMART by smart_unavailable, an early FAIL by
# whatever failed. "not_run" means the run never reached the test, which is
# already FAIL (a failed short test) or INCOMPLETE (the erase did not cover
# the drive), so repeating it as a CAUTION reason only adds noise.
_DELIBERATELY_NOT_RUN = ("skipped", "smart_unavailable",
                         "skipped_already_failed", "not_run")


def evaluate(report: dict, config: dict | None = None) -> Grade:
    """Compute the grade from a report.json-shaped dict.

    INCOMPLETE is checked first and short-circuits: it is a separate outcome,
    not a grade. A run that did not finish is an unfinished measurement, not
    evidence about the drive, so it must not be labelled FAIL.
    """
    config = config or load_config()
    rubric_version = config.get("rubric_version", 1)
    fail_cfg = config.get("fail", {})
    caution_cfg = config.get("caution", {})

    flags = report.get("flags") or {}
    conditions = report.get("run_conditions") or {}
    smart = report.get("smart") or {}
    attributes = smart.get("attributes") or []
    tests = report.get("self_tests") or {}
    short = tests.get("short") or {}
    extended = tests.get("extended") or {}
    erase = report.get("erase") or {}
    verify = report.get("verify") or {}
    events = conditions.get("kernel_events") or {}

    # ---- INCOMPLETE ------------------------------------------------------
    incomplete_reasons = []
    notes: list[str] = []
    if flags.get("thermally_aborted"):
        incomplete_reasons.append(
            conditions.get("thermal_abort_reason")
            or "run aborted by the thermal guard; the drive is not necessarily "
               "bad, the test conditions were"
        )
    if flags.get("interrupted"):
        incomplete_reasons.append(
            conditions.get("incomplete_reason")
            or "run was interrupted before it completed")
    if flags.get("too_many_disconnects"):
        incomplete_reasons.append(
            f"drive disconnected too many times "
            f"({conditions.get('disconnects', 0)}) to complete the run"
        )
    # The pipeline raises the interrupted flag for this itself; this is here so
    # a report built without it can never pass on a surface scan cut short.
    if extended.get("run") and extended.get("status") == "interrupted":
        incomplete_reasons.append(
            "the extended self-test was stopped before it finished")
    if incomplete_reasons:
        return Grade(INCOMPLETE, incomplete_reasons, rubric_version,
                     notes=notes)

    fail_reasons: list[str] = []
    caution_reasons: list[str] = []
    limitation_reasons: list[str] = []
    limitation_conditions = set(
        (caution_cfg.get('limitations') or {}).get('conditions') or [])

    def note_limitation(condition: str, text: str) -> None:
        caution_reasons.append(text)
        if condition in limitation_conditions:
            limitation_reasons.append(text)

    # ---- FAIL ------------------------------------------------------------
    if fail_cfg.get("smart_overall_health_failed") and smart.get("available"):
        if smart.get("overall_health") == "FAILED":
            fail_reasons.append(
                "FAIL: SMART overall-health self-assessment reports FAILED"
            )

    # Any status other than a pass or an unfinished test fails (see
    # smart.selftest_verdict), so smartctl wording nobody listed cannot pass.
    if fail_cfg.get("short_test_failed") and short.get("run"):
        if selftest_verdict(short.get("status")) == SELFTEST_FAILED:
            fail_reasons.append(f"FAIL: short self-test status = {short.get('status')}")

    if fail_cfg.get("extended_test_failed") and extended.get("run"):
        if selftest_verdict(extended.get("status")) == SELFTEST_FAILED:
            lba = extended.get("lba_of_first_error")
            suffix = f", first error at LBA {lba}" if lba is not None else ""
            fail_reasons.append(
                f"FAIL: extended self-test status = {extended.get('status')}{suffix}"
            )

    for attr_id, name in (fail_cfg.get("raw_gt_zero") or {}).items():
        raw = _raw(attributes, int(attr_id))
        if raw and raw > 0:
            fail_reasons.append(f"FAIL: {name} ({attr_id}) = {raw}")

    # NOT gated on verify.performed, and the asymmetry is the point:
    #
    #   "0 read errors"  means nothing unless the WHOLE device was read
    #   "38 read errors" means something however far the pass got
    #
    # A truncated pass -- --stop-on-fail, an interruption, a drive that
    # vanished -- sets performed=False so the report makes no positive
    # verification claim. Gating the failures on it too would find a drive bad
    # and then decline to fail it for what it found.
    if fail_cfg.get("any_read_error"):
        errors = verify.get("read_errors") or 0
        if errors:
            fail_reasons.append(
                f"FAIL: {errors} read error(s) during the full-surface read"
            )

    # Like read errors, not gated on erase.performed: an erase abandoned on
    # write failures found exactly what it was abandoned for. These used to
    # end the run as INCOMPLETE, which reads as "try again" about a drive
    # that cannot take writes.
    if fail_cfg.get("any_write_error"):
        write_errors = erase.get("write_errors") or 0
        if write_errors:
            fail_reasons.append(
                f"FAIL: {write_errors} region(s) could not be written during "
                f"the erase; data there was not overwritten"
                + ("; the erase was abandoned" if erase.get("write_abandoned")
                   else "")
            )

    if fail_cfg.get("any_nonzero_sector"):
        nonzero = verify.get("nonzero_ranges") or []
        if nonzero:
            fail_reasons.append(
                f"FAIL: {len(nonzero)} region(s) did not read back as zero; the "
                f"write did not take"
            )

    if fail_cfg.get("any_kernel_io_error"):
        io_errors = (events.get("io_errors") or 0) + (events.get("medium_errors") or 0)
        if io_errors:
            fail_reasons.append(
                f"FAIL: {io_errors} kernel-log I/O or medium error(s) for this "
                f"device during the run"
            )

    # ---- CAUTION ---------------------------------------------------------
    annotation = caution_cfg.get("cable_annotation", "")

    for attr_id, name in (caution_cfg.get("raw_gt_zero") or {}).items():
        raw = _raw(attributes, int(attr_id))
        if raw and raw > 0:
            caution_reasons.append(f"CAUTION: {name} ({attr_id}) = {raw}")

    for attr_id, name in (caution_cfg.get("raw_low16_gt_zero") or {}).items():
        raw = _raw(attributes, int(attr_id))
        if raw is not None and (raw & 0xFFFF) > 0:
            # Attribute 188 packs three counters into its raw field on many
            # drives; only the low 16 bits are the timeout counter (spec 10.1).
            caution_reasons.append(
                f"CAUTION: {name} ({attr_id}) = {raw & 0xFFFF}"
            )

    for attr_id, name in (caution_cfg.get("cable_symptom_raw_gt_zero") or {}).items():
        raw = _raw(attributes, int(attr_id))
        if raw and raw > 0:
            caution_reasons.append(
                f"CAUTION: {name} ({attr_id}) = {raw} ({annotation})"
            )

    # Events the operator has attributed to their own physical intervention --
    # unplugging a drive, swapping a cable -- are still recorded, and still
    # printed on the report. They are simply not counted against the drive,
    # because a deliberate reconnection says nothing about its condition.
    # Deleting them would hide a real event; blaming the drive for them would
    # misattribute one. Both mislead a buyer, in opposite directions.
    operator = conditions.get("operator_attributed_events") or {}
    for key, label in (caution_cfg.get("kernel_events") or {}).items():
        count = events.get(key) or 0
        excused = min(count, operator.get(key) or 0)
        remaining = count - excused
        if remaining:
            caution_reasons.append(
                f"CAUTION: {remaining} {label} event(s) in the kernel log "
                f"({annotation})"
            )
        if excused:
            notes.append(
                f"{excused} {label} event(s) were recorded and attributed by "
                f"the operator to deliberate physical intervention, not to the "
                f"drive. They are excluded from the grade."
            )

    hours = smart.get("power_on_hours")
    threshold = caution_cfg.get("power_on_hours")
    if hours is not None and threshold and hours > threshold:
        caution_reasons.append(
            f"CAUTION: power-on hours = {hours:,} (over {threshold:,})"
        )

    if caution_cfg.get("any_attribute_when_failed"):
        for attr in attributes:
            # smartctl's own when_failed field is a STRING: "" / "past" / "now".
            # Consuming it directly is the simplest correct implementation of
            # the threshold test, because many informational attributes carry
            # thresh = 0 meaning "no threshold" (spec 10.1).
            when = (attr.get("when_failed") or "").strip()
            if when:
                caution_reasons.append(
                    f"CAUTION: attribute {attr.get('id')} "
                    f"({attr.get('name')}) flagged failing {when}"
                )

    max_temp = conditions.get("temp_max_c")
    temp_threshold = caution_cfg.get("max_temp_c")
    if max_temp is not None and temp_threshold and max_temp > temp_threshold:
        caution_reasons.append(
            f"CAUTION: maximum temperature reached {max_temp} C "
            f"(over {temp_threshold} C)"
        )

    if caution_cfg.get("thermal_paused") and (conditions.get("thermal_pause_s") or 0) > 0:
        caution_reasons.append(
            f"CAUTION: thermal guard paused the run for "
            f"{conditions['thermal_pause_s']} s"
        )

    if caution_cfg.get("smart_unavailable") and not smart.get("available"):
        note_limitation(
            "smart_unavailable",
            "CAUTION: power-on hours and reallocated sector counts are unknown "
            "because SMART is not available through this drive's USB bridge"
        )

    if caution_cfg.get("extended_test_skipped") and flags.get("skipped_extended_test"):
        note_limitation("extended_test_skipped",
                        "CAUTION: extended self-test was skipped")

    # Previously the short test had no inconclusive rule while the extended
    # one did, which was an accident: both mean a health check could not be
    # read back.
    for kind, test in (("extended", extended), ("short", short)):
        status = test.get("status")
        if test.get("run"):
            if (caution_cfg.get(f"{kind}_test_inconclusive")
                    and selftest_verdict(status) not in (SELFTEST_PASSED,
                                                         SELFTEST_FAILED)
                    and status != "interrupted"):
                detail = ("" if status == "inconclusive"
                          else f" (the drive reported: {status})")
                note_limitation(
                    f"{kind}_test_inconclusive",
                    f"CAUTION: {kind} self-test was inconclusive; the drive "
                    f"accepted the test but the result could not be read "
                    f"back{detail}"
                )
        elif (caution_cfg.get(f"{kind}_test_not_run")
              and status not in _DELIBERATELY_NOT_RUN):
            # A test that could not even be started is as unmeasured as one
            # that was skipped. It used to add nothing, so a drive whose
            # extended test smartctl refused graded PASS without one.
            note_limitation(
                f"{kind}_test_not_run",
                f"CAUTION: {kind} self-test did not run ({status or 'no status'})"
            )

    if fail_reasons:
        return Grade(FAIL, fail_reasons + caution_reasons, rubric_version,
                     notes=notes)
    # Checked only once nothing has failed: a truncated pass that found bad
    # sectors is a FAIL (see above), but one that found nothing has not shown
    # the drive is clean, only that the part it covered was.
    unfinished = [label for label, block in (("erase", erase),
                                             ("verification", verify))
                  if not block.get("performed")]
    if unfinished:
        return Grade(INCOMPLETE, [
            f"the {' and '.join(unfinished)} did not cover the whole drive"
        ], rubric_version, notes=notes)
    if caution_reasons:
        return Grade(CAUTION, caution_reasons, rubric_version,
                     limitations=limitation_reasons, notes=notes)
    return Grade(PASS, [], rubric_version, notes=notes)
