"""Grading rubric: spec 14 test 10.

A hand-built report.json for each rubric condition, asserting both the grade
and the specific reason string.
"""

from __future__ import annotations

import copy

import pytest

from driveprep import grade as grading


def _attr(attr_id, raw, when_failed="", name=None):
    return {"id": attr_id, "name": name or f"Attr_{attr_id}", "value": 100,
            "worst": 100, "thresh": 0, "raw": raw, "flags": "PO--CK",
            "when_failed": when_failed}


def _with(clean_report, **sections):
    report = copy.deepcopy(clean_report)
    for key, value in sections.items():
        if isinstance(value, dict) and isinstance(report.get(key), dict):
            report[key] = {**report[key], **value}
        else:
            report[key] = value
    return report


# --------------------------------------------------------------------------


def test_10_a_fully_clean_drive_passes(clean_report, config):
    result = grading.evaluate(clean_report, config)
    assert result.value == grading.PASS
    assert result.reasons == []


@pytest.mark.parametrize("sections,fragment", [
    ({"smart": {"overall_health": "FAILED"}}, "overall-health"),
    ({"self_tests": {"short": {"run": True, "status": "read_failure"},
                     "extended": {"run": True,
                                  "status": "completed_without_error"}}},
     "short self-test"),
    ({"self_tests": {"short": {"run": True,
                               "status": "completed_without_error"},
                     "extended": {"run": True, "status": "servo_failure",
                                  "lba_of_first_error": 4096}}},
     "extended self-test"),
    ({"smart": {"attributes": [_attr(197, 5, name="Current_Pending_Sector")]}},
     "Current Pending Sector"),
    ({"smart": {"attributes": [_attr(198, 2, name="Offline_Uncorrectable")]}},
     "Offline Uncorrectable"),
    ({"smart": {"attributes": [_attr(187, 9)]}}, "Reported Uncorrectable"),
    ({"verify": {"read_errors": 12}}, "read error"),
    ({"verify": {"nonzero_ranges": [{"start_byte": 0, "length_bytes": 512}]}},
     "did not read back as zero"),
    ({"run_conditions": {"kernel_events": {"io_errors": 3, "medium_errors": 0,
                                           "usb_resets": 0, "uas_aborts": 0}}},
     "kernel-log I/O"),
    ({"run_conditions": {"kernel_events": {"io_errors": 0, "medium_errors": 1,
                                           "usb_resets": 0, "uas_aborts": 0}}},
     "kernel-log I/O"),
])
def test_10_fail_conditions(clean_report, config, sections, fragment):
    result = grading.evaluate(_with(clean_report, **sections), config)
    assert result.value == grading.FAIL
    assert any(fragment in reason for reason in result.reasons), result.reasons


@pytest.mark.parametrize("sections,fragment", [
    ({"smart": {"attributes": [_attr(5, 8)]}}, "Reallocated Sector Count"),
    ({"smart": {"attributes": [_attr(196, 3)]}}, "Reallocated Event Count"),
    ({"smart": {"attributes": [_attr(10, 1)]}}, "Spin Retry Count"),
    ({"smart": {"attributes": [_attr(199, 14)]}}, "UDMA CRC Error Count"),
    ({"smart": {"power_on_hours": 45000}}, "power-on hours"),
    ({"smart": {"attributes": [_attr(1, 0, when_failed="now")]}},
     "flagged failing"),
    ({"run_conditions": {"kernel_events": {"io_errors": 0, "medium_errors": 0,
                                           "usb_resets": 4, "uas_aborts": 0}}},
     "USB reset"),
    ({"run_conditions": {"kernel_events": {"io_errors": 0, "medium_errors": 0,
                                           "usb_resets": 0, "uas_aborts": 2}}},
     "UAS"),
    ({"run_conditions": {"thermal_pause_s": 300}}, "thermal guard paused"),
    ({"run_conditions": {"temp_max_c": 58}}, "maximum temperature"),
    ({"smart": {"available": False}}, "not available through this drive"),
    ({"flags": {"skipped_extended_test": True}}, "extended self-test was skipped"),
    ({"self_tests": {"short": {"run": True,
                               "status": "completed_without_error"},
                     "extended": {"run": True, "status": "inconclusive"}}},
     "inconclusive"),
])
def test_10_caution_conditions(clean_report, config, sections, fragment):
    result = grading.evaluate(_with(clean_report, **sections), config)
    assert result.value == grading.CAUTION
    assert any(fragment in reason for reason in result.reasons), result.reasons


def test_10_attribute_188_uses_only_the_low_16_bits(clean_report, config):
    """188 packs three counters into its raw field on many drives."""
    packed = (7 << 32) | (3 << 16) | 5
    result = grading.evaluate(
        _with(clean_report, smart={"attributes": [_attr(188, packed)]}), config)
    assert result.value == grading.CAUTION
    assert "= 5" in result.reasons[0], result.reasons

    zero_low = (7 << 32) | (3 << 16)
    assert grading.evaluate(
        _with(clean_report, smart={"attributes": [_attr(188, zero_low)]}),
        config).value == grading.PASS


def test_10_normalized_value_is_never_read_instead_of_raw(clean_report, config):
    """Reading attribute 5's normalized value would CAUTION every drive."""
    healthy = {"id": 5, "name": "Reallocated_Sector_Ct", "value": 200,
               "worst": 200, "thresh": 140, "raw": 0, "flags": "PO--CK",
               "when_failed": ""}
    result = grading.evaluate(
        _with(clean_report, smart={"attributes": [healthy]}), config)
    assert result.value == grading.PASS, \
        "a healthy drive reports normalized 200 and raw 0; the rubric reads raw"


def test_10_unknown_power_on_hours_does_not_fire_the_threshold(clean_report,
                                                               config):
    result = grading.evaluate(
        _with(clean_report, smart={"power_on_hours": None}), config)
    assert result.value == grading.PASS


@pytest.mark.parametrize("flag,fragment", [
    ("thermally_aborted", "thermal guard"),
    ("interrupted", "interrupted"),
    ("too_many_disconnects", "disconnected"),
])
def test_10_incomplete_is_a_separate_outcome(clean_report, config, flag,
                                             fragment):
    """A thermally aborted drive is not FAIL: the conditions were bad, not it."""
    result = grading.evaluate(_with(clean_report, flags={flag: True}), config)
    assert result.value == grading.INCOMPLETE
    assert any(fragment in reason for reason in result.reasons), result.reasons


def test_10_incomplete_wins_over_fail(clean_report, config):
    """An unfinished measurement is not evidence about the drive."""
    report = _with(clean_report, flags={"interrupted": True},
                   verify={"read_errors": 5})
    assert grading.evaluate(report, config).value == grading.INCOMPLETE


def test_10_fail_wins_over_caution(clean_report, config):
    report = _with(clean_report,
                   smart={"attributes": [_attr(197, 1), _attr(5, 4)]})
    result = grading.evaluate(report, config)
    assert result.value == grading.FAIL
    assert any("Current Pending" in r for r in result.reasons)
    assert any("Reallocated Sector" in r for r in result.reasons), \
        "every contributing number is still printed"


def test_10_cable_symptoms_carry_their_annotation(clean_report, config):
    result = grading.evaluate(
        _with(clean_report, smart={"attributes": [_attr(199, 3)]}), config)
    assert "cable" in result.reasons[0]


def test_10_grade_value_is_always_one_of_four(clean_report, config):
    for sections in ({}, {"verify": {"read_errors": 1}},
                     {"smart": {"attributes": [_attr(5, 1)]}},
                     {"flags": {"interrupted": True}}):
        assert grading.evaluate(_with(clean_report, **sections),
                                config).value in grading.VALID_GRADES


def test_10_rubric_version_is_stamped(clean_report, config):
    assert grading.evaluate(clean_report, config).rubric_version == \
        config["rubric_version"]


def test_10_unperformed_verify_does_not_contribute(clean_report, config):
    """A drive that was never erased must not be failed for 'no read errors'."""
    report = _with(clean_report,
                   erase={"performed": False},
                   verify={"performed": False, "read_errors": None,
                           "nonzero_ranges": []})
    assert grading.evaluate(report, config).value == grading.PASS


# --------------------------------------------------------------------------
# Limits of measurement vs findings about the drive
# --------------------------------------------------------------------------


def test_10_short_test_inconclusive_is_caution(clean_report, config):
    """Previously absent while the extended equivalent was present.

    Both mean the same thing: a health check could not be read back.
    """
    report = _with(clean_report, self_tests={
        "short": {"run": True, "status": "inconclusive"},
        "extended": {"run": True, "status": "completed_without_error"}})
    result = grading.evaluate(report, config)
    assert result.value == grading.CAUTION
    assert any("short self-test was inconclusive" in r for r in result.reasons)


def test_10_limitations_only_when_nothing_was_actually_found(clean_report,
                                                             config):
    report = _with(clean_report,
                   smart={"available": False, "attributes": []},
                   self_tests={"short": {"run": True, "status": "inconclusive"},
                               "extended": {"run": True,
                                            "status": "inconclusive"}})
    result = grading.evaluate(report, config)
    assert result.value == grading.CAUTION
    assert result.limitations_only is True
    assert len(result.limitations) == len(result.reasons)


@pytest.mark.parametrize("sections", [
    {"smart": {"attributes": [_attr(5, 8)]}},
    {"smart": {"attributes": [_attr(199, 3)]}},
    {"run_conditions": {"thermal_pause_s": 300}},
    {"smart": {"power_on_hours": 45000}},
])
def test_10_a_real_finding_is_never_limitations_only(clean_report, config,
                                                     sections):
    """One measured concern must switch the report back to a warning."""
    mixed = _with(clean_report, **sections)
    mixed = _with(mixed, flags={"skipped_extended_test": True})
    result = grading.evaluate(mixed, config)
    assert result.value == grading.CAUTION
    assert result.limitations_only is False, \
        "a measured finding must not be presented as a mere limitation"


def test_10_pass_and_fail_are_never_limitations_only(clean_report, config):
    assert grading.evaluate(clean_report, config).limitations_only is False
    failed = _with(clean_report, verify={"read_errors": 3})
    assert grading.evaluate(failed, config).limitations_only is False


def test_10_limitations_appear_in_the_serialised_grade(clean_report, config):
    report = _with(clean_report, flags={"skipped_extended_test": True})
    data = grading.evaluate(report, config).to_json()
    assert data["limitations_only"] is True
    assert data["limitations"] == data["reasons"]
    assert data["value"] == grading.CAUTION, \
        "the grade itself is unchanged; only the framing differs"


# --------------------------------------------------------------------------
# Truncated runs: findings still count
# --------------------------------------------------------------------------


def test_10_read_errors_fail_even_when_the_verify_was_truncated(clean_report,
                                                                config):
    """"0 errors" needs full coverage to mean anything; "38 errors" does not.

    --stop-on-fail sets verify.performed False, so the report makes no positive
    verification claim. Gating the FAIL on it too would find a drive bad and
    then decline to fail it for what it found.
    """
    report = _with(clean_report, verify={
        "performed": False, "read_errors": 38, "nonzero_ranges": [],
        "stopped_on_fail": True})
    result = grading.evaluate(report, config)
    assert result.value == grading.FAIL
    assert any("38 read error" in r for r in result.reasons)


def test_10_nonzero_regions_fail_even_when_truncated(clean_report, config):
    report = _with(clean_report, verify={
        "performed": False, "read_errors": 0,
        "nonzero_ranges": [{"first_lba": 8}], "stopped_on_fail": True})
    assert grading.evaluate(report, config).value == grading.FAIL


def test_10_an_unperformed_verify_with_no_findings_does_not_fail(clean_report,
                                                                 config):
    """Absence of a verify must not be mistaken for evidence against a drive."""
    report = _with(clean_report, verify={
        "performed": False, "read_errors": None, "nonzero_ranges": []})
    assert grading.evaluate(report, config).value != grading.FAIL


# --------------------------------------------------------------------------
# Operator-attributed run conditions
# --------------------------------------------------------------------------


def _events(**counts):
    base = {"io_errors": 0, "medium_errors": 0, "usb_resets": 0,
            "uas_aborts": 0}
    base.update(counts)
    return base


def test_10_an_operator_caused_disconnect_does_not_grade_against_the_drive(
        clean_report, config):
    """Pulling the cable yourself says nothing about the hardware.

    The event is real and stays on the report; it just is not charged to the
    drive. Blaming it would tell a buyer the hardware glitched when a human
    unplugged it.
    """
    report = _with(clean_report,
                   run_conditions={"kernel_events": _events(usb_resets=1),
                                   "operator_attributed_events":
                                       {"usb_resets": 1}})
    result = grading.evaluate(report, config)
    assert result.value == grading.PASS
    assert result.reasons == []
    assert any("operator" in n for n in result.notes), result.notes
    assert any("excluded from the grade" in n for n in result.notes)


def test_10_an_excused_event_is_still_disclosed(clean_report, config):
    """Not grading on it is not the same as hiding it."""
    report = _with(clean_report,
                   run_conditions={"kernel_events": _events(usb_resets=1),
                                   "operator_attributed_events":
                                       {"usb_resets": 1}})
    data = grading.evaluate(report, config).to_json()
    assert data["notes"], "the event must still appear on the report"
    assert "1 USB reset or disconnect event(s) were recorded" in data["notes"][0]


def test_10_only_the_attributed_share_is_excused(clean_report, config):
    """86 resets with 1 admitted stays a CAUTION about the remaining 85."""
    report = _with(clean_report,
                   run_conditions={"kernel_events": _events(usb_resets=86),
                                   "operator_attributed_events":
                                       {"usb_resets": 1}})
    result = grading.evaluate(report, config)
    assert result.value == grading.CAUTION
    assert any("85 USB reset" in r for r in result.reasons), result.reasons
    assert any("1 USB reset" in n for n in result.notes), result.notes


def test_10_over_attribution_cannot_go_negative(clean_report, config):
    report = _with(clean_report,
                   run_conditions={"kernel_events": _events(usb_resets=1),
                                   "operator_attributed_events":
                                       {"usb_resets": 99}})
    result = grading.evaluate(report, config)
    assert result.value == grading.PASS
    assert not any("-" in r for r in result.reasons)


def test_10_attribution_does_not_excuse_a_different_category(clean_report,
                                                              config):
    """Admitting you unplugged it does not explain away UAS aborts."""
    report = _with(clean_report,
                   run_conditions={"kernel_events": _events(usb_resets=1,
                                                            uas_aborts=4),
                                   "operator_attributed_events":
                                       {"usb_resets": 1}})
    result = grading.evaluate(report, config)
    assert result.value == grading.CAUTION
    assert any("UAS" in r for r in result.reasons), result.reasons


def test_10_findings_about_the_drive_are_never_excusable(clean_report, config):
    """Only run conditions can be attributed; media findings cannot."""
    report = _with(clean_report,
                   verify={"read_errors": 5},
                   run_conditions={"kernel_events": _events(usb_resets=1),
                                   "operator_attributed_events":
                                       {"usb_resets": 1, "read_errors": 5}})
    assert grading.evaluate(report, config).value == grading.FAIL
