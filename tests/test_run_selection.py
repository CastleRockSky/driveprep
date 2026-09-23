"""`--id` must resolve to the run the operator meant, or refuse.

Written after `print --id <bay-name>` put the wrong drive's report on paper:
a CAUTION page for one drive when asked for another that had graded FAIL.
"""
import json
import types

import pytest

from driveprep import runs as main


BAY = "usb-Test_Model_2TB-00A00A00_ENCL0000000-0_1"
SER_A = "WD-TESTAAAA0001"
SER_B = "WD-TESTBBBB0002"


def _run_dir(root, name, serial, grade="PASS", by_id=None):
    """A stored run, shaped like the ones on disk."""
    directory = root / name
    directory.mkdir()
    (directory / "report.json").write_text(json.dumps({
        "drive": {
            "by_id": by_id or f"{BAY.replace('_1', ':1')}",
            "ata_serial": serial,
            "enclosure_serial": "ENCL0000000",
            "model": "Test Model 2TB",
            "capacity_label": "2 TB",
        },
        "grade": {"value": grade, "reasons": []},
    }), encoding="utf-8")
    return directory


def _options(root, ids):
    return types.SimpleNamespace(output_root=str(root), ids=list(ids))


# --------------------------------------------------------------------------
# The failure that reached paper
# --------------------------------------------------------------------------


def test_a_bay_name_that_names_three_runs_is_refused(tmp_path):
    """The regression. Three drives have used this bay; the bay names none.

    Before the fix the pre-serial directory won on an exact name match, so
    the request silently resolved to whichever drive ran there first.
    """
    _run_dir(tmp_path, BAY, "WD-TESTCCCC0003", grade="CAUTION")
    _run_dir(tmp_path, f"{BAY}__{SER_A}", SER_A, grade="FAIL")
    _run_dir(tmp_path, f"{BAY}__{SER_B}", SER_B, grade="PASS")

    targets, matched, ambiguous = main.select_runs({BAY}, tmp_path)

    assert targets == [], "an ambiguous request must select nothing"
    assert matched == set()
    assert len(ambiguous) == 1
    # The refusal has to be actionable: the pre-serial directory's own name IS
    # the ambiguous string, so serials are what breaks the tie.
    assert SER_A in ambiguous[0] and SER_B in ambiguous[0]
    assert "WD-TESTCCCC0003" in ambiguous[0]


def test_the_serial_resolves_to_exactly_one_run(tmp_path):
    _run_dir(tmp_path, BAY, "WD-TESTCCCC0003", grade="CAUTION")
    _run_dir(tmp_path, f"{BAY}__{SER_A}", SER_A, grade="FAIL")
    _run_dir(tmp_path, f"{BAY}__{SER_B}", SER_B, grade="PASS")

    for serial, expected in ((SER_A, f"{BAY}__{SER_A}"),
                             (SER_B, f"{BAY}__{SER_B}"),
                             ("WD-TESTCCCC0003", BAY)):
        targets, matched, ambiguous = main.select_runs({serial}, tmp_path)
        assert not ambiguous
        assert [d.name for d in targets] == [expected], serial
        assert matched == {serial}


def test_the_full_directory_name_resolves_to_itself(tmp_path):
    _run_dir(tmp_path, BAY, "WD-TESTCCCC0003")
    _run_dir(tmp_path, f"{BAY}__{SER_A}", SER_A)

    targets, matched, ambiguous = main.select_runs(
        {f"{BAY}__{SER_A}"}, tmp_path)
    assert not ambiguous
    assert [d.name for d in targets] == [f"{BAY}__{SER_A}"]


def test_a_by_id_naming_one_run_still_resolves(tmp_path):
    """Direct-USB drives were never ambiguous and must not regress."""
    name = "usb-WD_My_Passport_0730_5445535453455249414c3031-0_0"
    _run_dir(tmp_path, name, "WD-TESTAAAA0001",
             by_id=name.replace("-0_0", "-0:0"))

    targets, matched, ambiguous = main.select_runs({name}, tmp_path)
    assert not ambiguous
    assert [d.name for d in targets] == [name]


def test_no_ids_selects_every_stored_run(tmp_path):
    _run_dir(tmp_path, BAY, "WD-TESTCCCC0003")
    _run_dir(tmp_path, f"{BAY}__{SER_A}", SER_A)
    (tmp_path / "batches").mkdir()

    targets, matched, ambiguous = main.select_runs(set(), tmp_path)
    assert not ambiguous
    assert [d.name for d in targets] == [BAY, f"{BAY}__{SER_A}"]
    assert "batches" not in [d.name for d in targets]


def test_an_unknown_id_matches_nothing_and_is_not_ambiguous(tmp_path):
    _run_dir(tmp_path, BAY, "WD-TESTCCCC0003")
    targets, matched, ambiguous = main.select_runs({"NOSUCHDRIVE"}, tmp_path)
    assert targets == [] and matched == set() and ambiguous == []


def test_a_run_with_no_report_still_answers_to_its_serial(tmp_path):
    """A killed run leaves state.json but no report.json."""
    directory = tmp_path / f"{BAY}__{SER_A}"
    directory.mkdir()
    (directory / "state.json").write_text(json.dumps({
        "drive": {"by_id": BAY.replace("_1", ":1"), "ata_serial": SER_A},
    }), encoding="utf-8")

    targets, _, ambiguous = main.select_runs({SER_A}, tmp_path)
    assert not ambiguous
    assert [d.name for d in targets] == [f"{BAY}__{SER_A}"]


def test_unreadable_json_does_not_take_the_command_down(tmp_path):
    directory = tmp_path / BAY
    directory.mkdir()
    (directory / "report.json").write_text("{ truncated", encoding="utf-8")

    aliases = main.stored_aliases(directory)
    assert aliases == {BAY}, "a corrupt report leaves the directory name"
    targets, _, ambiguous = main.select_runs({BAY}, tmp_path)
    assert [d.name for d in targets] == [BAY]
    assert not ambiguous
