"""Agent state machine and run-ownership lease.

State machine:
    CREATED -> [PREPARING ->] STARTING -> RUNNING -> {COMPLETED | FAILED | TIMED_OUT | CANCELLED}
    (PREPARING is used by Phase 2 tasks only.)
    CREATED/PREPARING/STARTING may also go straight to FAILED or CANCELLED.
    Any terminal state -> CLEANUP -> REMOVED.

Run ownership: every Orchestrator holds an exclusive flock on
`<state_dir>/<run_id>.lock` for its lifetime. The kernel releases the lock when the
process dies (even on SIGKILL), so "lock is free" reliably means "that run is stale"
while "lock is held" means "that run is still active". Linux/POSIX only.
"""
from __future__ import annotations

import fcntl
import os
import threading
import time
from enum import Enum
from pathlib import Path


class AgentState(str, Enum):
    CREATED = "CREATED"
    PREPARING = "PREPARING"  # Phase 2 only: workspace/credentials/runtime inputs being prepared
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    CLEANUP = "CLEANUP"
    REMOVED = "REMOVED"


TERMINAL_STATES = frozenset(
    {AgentState.COMPLETED, AgentState.FAILED, AgentState.TIMED_OUT, AgentState.CANCELLED}
)

_S = AgentState
TRANSITIONS = {
    _S.CREATED: {_S.PREPARING, _S.STARTING, _S.FAILED, _S.CANCELLED},
    _S.PREPARING: {_S.STARTING, _S.FAILED, _S.CANCELLED},
    _S.STARTING: {_S.RUNNING, _S.FAILED, _S.CANCELLED},
    _S.RUNNING: {_S.COMPLETED, _S.FAILED, _S.TIMED_OUT, _S.CANCELLED},
    **{state: {_S.CLEANUP} for state in TERMINAL_STATES},
    _S.CLEANUP: {_S.REMOVED},
    _S.REMOVED: set(),
}


class InvalidTransition(RuntimeError):
    pass


class StateMachine:
    """Thread-safe agent state holder that rejects illegal transitions."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = AgentState.CREATED
        self.history = [(self._state, time.time())]

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state in TERMINAL_STATES

    def transition(self, new_state: AgentState) -> AgentState:
        with self._lock:
            if new_state not in TRANSITIONS[self._state]:
                raise InvalidTransition(f"illegal transition {self._state.value} -> {new_state.value}")
            self._state = new_state
            self.history.append((new_state, time.time()))
            return new_state


class LeaseHeldError(RuntimeError):
    pass


class RunLease:
    """Exclusive, crash-safe claim on a run ID."""

    def __init__(self, path: Path, fd: int):
        self.path = path
        self._fd = fd

    @staticmethod
    def lease_path(state_dir: Path, run_id: str) -> Path:
        return Path(state_dir) / f"{run_id}.lock"

    @classmethod
    def acquire(cls, state_dir: Path, run_id: str) -> "RunLease":
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        path = cls.lease_path(state_dir, run_id)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LeaseHeldError(f"run {run_id} is already active")
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        return cls(path, fd)

    @staticmethod
    def is_active(state_dir: Path, run_id: str) -> bool:
        """True only if some live process currently holds the lease for run_id."""
        path = RunLease.lease_path(state_dir, run_id)
        try:
            fd = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)  # also releases the probe lock
        return False

    @staticmethod
    def forget(state_dir: Path, run_id: str) -> None:
        try:
            RunLease.lease_path(state_dir, run_id).unlink()
        except FileNotFoundError:
            pass

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        os.close(self._fd)
        self._fd = None
