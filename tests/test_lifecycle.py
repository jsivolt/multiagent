import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from orchestrator.lifecycle import (
    TERMINAL_STATES,
    TRANSITIONS,
    AgentState as S,
    InvalidTransition,
    LeaseHeldError,
    RunLease,
    StateMachine,
)


def walk(sm, *states):
    for s in states:
        sm.transition(s)


@pytest.mark.parametrize("terminal", [S.COMPLETED, S.FAILED, S.TIMED_OUT, S.CANCELLED])
def test_every_terminal_state_reaches_removed(terminal):
    sm = StateMachine()
    walk(sm, S.STARTING, S.RUNNING, terminal, S.CLEANUP, S.REMOVED)
    assert sm.state is S.REMOVED


def test_early_failure_and_cancel_paths():
    for early in (S.FAILED, S.CANCELLED):
        sm = StateMachine()
        walk(sm, early, S.CLEANUP, S.REMOVED)
    sm = StateMachine()
    walk(sm, S.STARTING, S.FAILED)  # e.g. docker start error


@pytest.mark.parametrize(
    "path, bad",
    [
        ((), S.RUNNING),  # cannot skip STARTING
        ((), S.COMPLETED),
        ((S.STARTING, S.RUNNING, S.COMPLETED), S.RUNNING),  # terminal is final
        ((S.STARTING, S.RUNNING, S.COMPLETED), S.FAILED),  # no terminal -> terminal
        ((S.STARTING, S.RUNNING, S.CANCELLED), S.REMOVED),  # must go through CLEANUP
        ((S.STARTING, S.RUNNING, S.FAILED, S.CLEANUP, S.REMOVED), S.CLEANUP),
    ],
)
def test_illegal_transitions_rejected(path, bad):
    sm = StateMachine()
    walk(sm, *path)
    before = sm.state
    with pytest.raises(InvalidTransition):
        sm.transition(bad)
    assert sm.state is before


def test_transition_table_is_closed_and_terminals_only_cleanup():
    assert set(TRANSITIONS) == set(S)
    for t in TERMINAL_STATES:
        assert TRANSITIONS[t] == {S.CLEANUP}
    assert TRANSITIONS[S.REMOVED] == set()


def test_history_records_order():
    sm = StateMachine()
    walk(sm, S.STARTING, S.RUNNING)
    assert [s for s, _ in sm.history] == [S.CREATED, S.STARTING, S.RUNNING]


# ---- run lease -------------------------------------------------------------------
def test_lease_active_while_held_and_stale_after_release(tmp_path):
    assert not RunLease.is_active(tmp_path, "r1")  # never existed
    lease = RunLease.acquire(tmp_path, "r1")
    assert RunLease.is_active(tmp_path, "r1")
    with pytest.raises(LeaseHeldError):
        RunLease.acquire(tmp_path, "r1")
    lease.release()
    assert not RunLease.is_active(tmp_path, "r1")


def test_lease_becomes_stale_when_owner_is_killed(tmp_path):
    code = textwrap.dedent(
        f"""
        import sys, time
        from orchestrator.lifecycle import RunLease
        RunLease.acquire(r"{tmp_path}", "dead-run")
        print("ready", flush=True)
        time.sleep(60)
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                            env={**os.environ, "PYTHONPATH": os.getcwd()})
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert RunLease.is_active(tmp_path, "dead-run")
        proc.send_signal(signal.SIGKILL)  # no chance to clean up
        proc.wait(timeout=5)
        deadline = time.time() + 5
        while RunLease.is_active(tmp_path, "dead-run") and time.time() < deadline:
            time.sleep(0.05)
        assert not RunLease.is_active(tmp_path, "dead-run")
    finally:
        proc.kill()
