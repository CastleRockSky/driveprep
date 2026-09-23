"""run_selftest against a scripted smartctl.

What matters is when the loop decides a test has started and finished, and
which log entry it then believes. Each case here once produced a result for a
test other than the one it started.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from driveprep import smart


def _entry(kind="Extended offline", string="Completed without error",
           passed=True, hours=9999, lba=None):
    return {"type": {"string": kind},
            "status": {"string": string, "passed": passed},
            "lifetime_hours": hours, "lba": lba}


OLD = _entry(hours=1000)


class FakeSmartctl:
    """Scripted smartctl: -c answers pop from `polls`; -l selftest reads `log`.

    A poll is "unreadable", "idle", or an int (remaining_percent). A poll may
    also be a (state, new_log) pair, which replaces the log as it is read --
    the drive writing its result.
    """

    def __init__(self, polls, start_rc=0, log=None):
        self.polls = list(polls)
        self.start_rc = start_rc
        self.log = log if log is not None else {"count": 1, "table": [OLD]}
        self.calls = []

    def __call__(self, args, timeout=120):
        self.calls.append(args)
        if "-t" in args:
            return subprocess.CompletedProcess(args, self.start_rc, "", "")
        if "-X" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        if "-l" in args:
            body = {"ata_smart_self_test_log": {"standard": self.log}} \
                if self.log else {}
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        poll = self.polls.pop(0) if self.polls else "idle"
        if isinstance(poll, tuple):
            poll, self.log = poll
        if poll == "unreadable":
            return subprocess.CompletedProcess(args, 0, "not json", "")
        status = {"value": 0, "string": "completed"}
        if isinstance(poll, int):
            status = {"value": 249, "remaining_percent": poll}
        body = {"ata_smart_data": {"self_test": {"status": status}}}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(smart.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(smart.time, "sleep", fake.sleep)
    return fake


def _run(monkeypatch, fake, kind="long", should_stop=None):
    monkeypatch.setattr(smart, "_run", fake)
    return smart.run_selftest("/dev/sdx", "sat", kind, poll_interval_s=300,
                              estimated_minutes=60, should_stop=should_stop)


def _new_log(entry):
    return {"count": 2, "table": [entry, OLD]}


def test_a_normal_test_reads_the_new_entry(monkeypatch, clock):
    fake = FakeSmartctl([90, 50, ("idle", _new_log(_entry(
        string="Completed: read failure", passed=False, lba=884736)))])
    result = _run(monkeypatch, fake)
    assert result.status == "completed:_read_failure"
    assert result.lba_of_first_error == 884736
    assert result.fatal


@pytest.mark.parametrize("rc", [64, 128, 192, 4 | 64])
def test_old_errors_in_the_drive_logs_do_not_mean_the_test_failed_to_start(
        monkeypatch, clock, rc):
    """smartctl's exit status is a bitmask; bits 6-7 are drive history."""
    fake = FakeSmartctl([50, ("idle", _new_log(_entry()))], start_rc=rc)
    result = _run(monkeypatch, fake)
    assert result.run
    assert result.status == "completed_without_error"


@pytest.mark.parametrize("rc", [1, 2])
def test_a_real_start_failure_is_reported(monkeypatch, clock, rc):
    result = _run(monkeypatch, FakeSmartctl([], start_rc=rc))
    assert not result.run
    assert result.status.startswith("could_not_start")


def test_an_unreadable_poll_is_not_taken_as_finished(monkeypatch, clock):
    """This used to end the poll and report the previous test's pass."""
    fake = FakeSmartctl(["unreadable"] * 1000)
    result = _run(monkeypatch, fake)
    assert result.status == "inconclusive"


def test_an_idle_drive_with_no_new_entry_is_inconclusive(monkeypatch, clock):
    """Accepted -t, never ran: the old entry is not this test's result."""
    result = _run(monkeypatch, FakeSmartctl(["idle"] * 1000))
    assert result.status == "inconclusive"


def test_a_timed_out_poll_is_survived(monkeypatch, clock):
    """One slow answer from a USB bridge must not end a nine-hour test."""
    fake = FakeSmartctl([90, 80, ("idle", _new_log(_entry()))])
    inner = fake.__call__
    timed_out = []

    def flaky(args, timeout=120):
        if "-c" in args and not timed_out:
            timed_out.append(True)
            raise smart.SmartError("smartctl failed: timed out")
        return inner(args, timeout)

    result = _run(monkeypatch, flaky)
    assert result.status == "completed_without_error"


def test_a_new_entry_of_another_kind_is_not_this_result(monkeypatch, clock):
    fake = FakeSmartctl([50, ("idle", _new_log(_entry(kind="Short offline")))])
    assert _run(monkeypatch, fake).status == "inconclusive"


def test_without_a_log_from_before_the_drive_must_be_seen_running(
        monkeypatch, clock):
    """No baseline means "new" cannot be judged from the log alone."""
    fake = FakeSmartctl(["unreadable", ("unreadable", _new_log(_entry()))]
                        + ["unreadable"] * 1000, log={})
    assert _run(monkeypatch, fake).status == "inconclusive"

    fake = FakeSmartctl([40, ("idle", _new_log(_entry()))], log={})
    assert _run(monkeypatch, fake).status == "completed_without_error"


def test_a_stop_aborts_the_test_on_the_drive(monkeypatch, clock):
    """Otherwise the drive keeps scanning after the run has walked away."""
    fake = FakeSmartctl([90, 80])
    result = _run(monkeypatch, fake, should_stop=lambda: len(fake.calls) > 3)
    assert result.status == "interrupted"
    assert any("-X" in args for args in fake.calls)


def test_the_short_kind_matches_a_short_entry(monkeypatch, clock):
    fake = FakeSmartctl([50, ("idle", _new_log(_entry(kind="Short offline")))])
    assert _run(monkeypatch, fake, kind="short").status == \
        "completed_without_error"


def test_a_wedged_test_is_aborted_and_rerun(monkeypatch, clock):
    """Stuck at 10% with the drive still "in progress": abort, run it again.

    Seen on a real drive: the first test hung at 90% and was never logged; an
    abort and a fresh test completed on schedule.
    """
    fake = FakeSmartctl([90, 10] + [10] * 200)   # wedges at 10% remaining
    starts = []
    inner = fake.__call__

    def smartctl(args, timeout=120):
        if "-t" in args:
            starts.append(True)
            if len(starts) == 2:   # the re-run behaves
                fake.polls = [50, ("idle", _new_log(_entry()))]
        return inner(args, timeout)

    monkeypatch.setattr(smart, "_run", smartctl)
    result = smart.run_selftest("/dev/sdx", "sat", "long", poll_interval_s=300,
                                estimated_minutes=60, stall_retries=1)
    assert len(starts) == 2
    assert any("-X" in a for a in fake.calls), "the wedged test was aborted"
    assert result.status == "completed_without_error"


def test_a_wedged_retry_that_wedges_again_is_inconclusive(monkeypatch, clock):
    fake = FakeSmartctl([10] * 1000)
    result = _run(monkeypatch, fake)   # stall_retries defaults to 0
    assert result.status == "inconclusive"
    assert any("-X" in a for a in fake.calls), \
        "a wedged test is not left holding the drive"


def test_a_silent_bridge_is_not_retried(monkeypatch, clock):
    """No progress because nothing is reported: a retry would report nothing."""
    fake = FakeSmartctl(["unreadable"] * 1000)
    monkeypatch.setattr(smart, "_run", fake)
    smart.run_selftest("/dev/sdx", "sat", "long", poll_interval_s=300,
                       estimated_minutes=60, stall_retries=1)
    assert sum("-t" in a for a in fake.calls) == 1


def test_a_hang_at_ninety_percent_is_caught_in_hours(monkeypatch, clock):
    """The observed case waited 24.7 h; a 493-minute test now gives up in ~4."""
    fake = FakeSmartctl([90, 50, 10] + [10] * 10000)
    monkeypatch.setattr(smart, "_run", fake)
    smart.run_selftest("/dev/sdx", "sat", "long", poll_interval_s=300,
                       estimated_minutes=493)
    assert clock.now < 6 * 3600


def test_a_stop_is_noticed_within_seconds_not_a_whole_poll(monkeypatch, clock):
    """A 300 s poll sleep used to ignore Ctrl+C for up to five minutes."""
    fake = FakeSmartctl([90] * 100)
    stop_at = 30.0
    result = _run(monkeypatch, fake,
                  should_stop=lambda: clock.now >= stop_at)
    assert result.status == "interrupted"
    assert clock.now < stop_at + 5
