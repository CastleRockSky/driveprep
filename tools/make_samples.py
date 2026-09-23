"""Regenerate samples/*.png from synthetic reports.

    python3 tools/make_samples.py

Every value here is synthetic. The samples were once rendered from a report
built on real hardware, and its by-id name -- which carries an Elements
enclosure's hex-encoded drive serial -- sat in the footer of every image in
this public repo. A PNG cannot be scanned for serials as text, so the rule is
that samples come only from this script, which tests/test_no_real_serials.py
does scan.

The base report is the worked example in docs/SPEC.md section 12, so the
samples and the spec cannot drift apart. The grade on each sample is computed
by the real rubric, never written by hand.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from driveprep import grade as grading, report as reporting, smart  # noqa: E402

SAMPLES = REPO / "samples"


def _spec_example() -> dict:
    """The section 12 report.json example, parsed from the spec itself."""
    text = (REPO / "docs" / "SPEC.md").read_text(encoding="utf-8")
    for block in re.findall(r"```json\n(.*?)```", text, re.S):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "report_id" in data:
            return data
    raise SystemExit("no report.json example found in docs/SPEC.md")


def _attributes() -> list[dict]:
    """A real-shaped attribute table from the synthetic bridged fixture."""
    fixture = REPO / "tests" / "fixtures" / "smart-usb-bridged.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    return smart.SmartResult(True, "sat", data).attributes


def _set_raw(report: dict, attr_id: int, raw: int, name: str) -> None:
    attrs = report["smart"]["attributes"]
    for attr in attrs:
        if attr["id"] == attr_id:
            attr["raw"] = raw
            return
    attrs.append({"id": attr_id, "name": name, "value": 100, "worst": 100,
                  "thresh": 0, "raw": raw, "flags": "-O--CK",
                  "when_failed": ""})
    attrs.sort(key=lambda a: a["id"])


def _events(**counts) -> dict:
    return {"io_errors": 0, "medium_errors": 0, "usb_resets": 0,
            "uas_aborts": 0, **counts}


def _not_erased(report: dict, reason: str) -> None:
    report["erase"] = {"performed": False, "not_performed_reason": reason,
                       "bytes_written": None}
    report["verify"] = {"performed": False, "bytes_read": None,
                        "nonzero_ranges": [], "read_error_ranges": [],
                        "read_errors": None, "result": None}


def pass_(r):
    pass


def caution(r):
    r["smart"]["power_on_hours"] = 46512
    _set_raw(r, 9, 46512, "Power_On_Hours")
    _set_raw(r, 199, 3, "UDMA_CRC_Error_Count")
    _set_raw(r, 188, 2, "Command_Timeout")
    r["run_conditions"]["kernel_events"] = _events(usb_resets=6, uas_aborts=2)


def fail(r):
    _set_raw(r, 5, 96, "Reallocated_Sector_Ct")
    _set_raw(r, 197, 24, "Current_Pending_Sector")
    r["verify"].update(
        read_errors=7, result="errors_found",
        read_error_ranges=[{"start_byte": 1_881_235_456_000 + i * 4096,
                            "length_bytes": 4096, "first_lba": None}
                           for i in range(7)])
    r["self_tests"]["extended"].update(
        status="completed:_read_failure", lba_of_first_error=3674288000)
    r["run_conditions"]["kernel_events"] = _events(medium_errors=7)


def notreased(r):
    r["self_tests"] = {
        "short": {"run": True, "status": "completed:_read_failure",
                  "duration_s": 64, "lba_of_first_error": 1_024_000},
        "extended": {"run": False, "status": "not_run"}}
    _not_erased(r, "short self-test: completed:_read_failure")


def nosmart(r):
    r["smart"] = {"available": False, "attributes": [], "probe_log": [
        {"d_type": t, "accepted": False, "reason": "no SMART data returned"}
        for t in ("auto", "sat", "sat,12", "usbjmicron")]}
    r["drive"].update(model="Elements 25A2", vendor=None, rotation_rpm=None,
                      form_factor=None, firmware=None, ata_serial=None)
    r["self_tests"] = {"short": {"run": False, "status": "smart_unavailable"},
                       "extended": {"run": False, "status": "smart_unavailable"}}
    r["flags"]["smart_via_bridge_unavailable"] = True


def incomplete(r):
    reason = ("temperature stayed at or above 60 C for more than 5 minutes "
              "despite pausing (peak 62 C). Improve airflow.")
    r["run_conditions"].update(temp_max_c=62, thermal_pause_s=1260,
                               thermal_abort_reason=reason)
    r["verify"].update(performed=False, bytes_read=1_203_486_720_000,
                       result=None)
    r["self_tests"]["extended"] = {"run": False, "status": "not_run"}
    r["flags"]["thermally_aborted"] = True


def many(r):
    """More attributes than the table holds: it must truncate visibly."""
    extra = [(183, "Runtime_Bad_Block"), (184, "End-to-End_Error"),
             (187, "Reported_Uncorrect"), (189, "High_Fly_Writes"),
             (190, "Airflow_Temperature_Cel"), (191, "G-Sense_Error_Rate"),
             (195, "Hardware_ECC_Recovered"), (220, "Disk_Shift"),
             (222, "Loaded_Hours"), (223, "Load_Retry_Count"),
             (224, "Load_Friction"), (226, "Load-in_Time"),
             (240, "Head_Flying_Hours"), (241, "Total_LBAs_Written"),
             (242, "Total_LBAs_Read"), (250, "Read_Error_Retry_Rate"),
             (254, "Free_Fall_Sensor")]
    for attr_id, name in extra:
        _set_raw(r, attr_id, 0, name)


SCENARIOS = {"pass": pass_, "caution": caution, "fail": fail,
             "notreased": notreased, "nosmart": nosmart,
             "incomplete": incomplete, "many": many}


def build(name: str) -> dict:
    report = copy.deepcopy(BASE)
    SCENARIOS[name](report)
    report["grade"] = grading.evaluate(report).to_json()
    return report


BASE = _spec_example()
BASE["smart"]["attributes"] = _attributes()


def main() -> int:
    work = SAMPLES / ".build"
    work.mkdir(parents=True, exist_ok=True)
    failed = []
    for name in SCENARIOS:
        report = build(name)
        html = reporting.render_html(report, work / f"{name}.html")
        if not reporting.render_png(html, SAMPLES / f"{name}.png"):
            failed.append(name)
        print(f"{name:11} {report['grade']['value']}")
    for path in work.iterdir():
        path.unlink()
    work.rmdir()
    if failed:
        print(f"PNG rendering failed for: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
