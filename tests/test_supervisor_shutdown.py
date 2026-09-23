"""The parent must not wait forever for a child that will not stop."""
import time

from driveprep import supervisor as sup


class FakeProc:
    def __init__(self):
        self.killed = False

    def is_alive(self):
        return not self.killed

    def kill(self):
        self.killed = True


def _supervisor(grace):
    class Opts:
        jobs = 1
    return sup.Supervisor({"shutdown": {"child_grace_s": grace}}, Opts())


def test_a_child_past_the_grace_period_is_killed():
    s = _supervisor(grace=60)
    s._children = {"d": FakeProc()}
    s._stopping = True
    s._last_interrupt = time.monotonic() - 61
    s._kill_overdue_children()
    assert s._children["d"].killed


def test_a_child_inside_the_grace_period_is_left_to_checkpoint():
    s = _supervisor(grace=60)
    s._children = {"d": FakeProc()}
    s._stopping = True
    s._last_interrupt = time.monotonic() - 10
    s._kill_overdue_children()
    assert not s._children["d"].killed


def test_nothing_is_killed_without_a_stop_request():
    s = _supervisor(grace=0)
    s._children = {"d": FakeProc()}
    s._kill_overdue_children()
    assert not s._children["d"].killed
