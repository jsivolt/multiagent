"""Data models and constants for the Phase 1 orchestrator."""
from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .lifecycle import AgentState, StateMachine

# Ownership labels. A container/volume is only ever cleaned up if it carries BOTH the
# project label and a run-id label whose run is stale (see Orchestrator.cleanup_stale).
LABEL_PROJECT = "multiagent.poc"
LABEL_PROJECT_VALUE = "phase1"
LABEL_RUN_ID = "multiagent.poc.run_id"
LABEL_AGENT_ID = "multiagent.poc.agent_id"
LABEL_TASK_ID = "multiagent.poc.task_id"
LABEL_CREATED_AT = "multiagent.poc.created_at"
LABEL_KIND = "multiagent.poc.kind"  # "agent-task": Phase 2 containers, reconciled (not blindly removed) after a crash
KIND_TASK = "agent-task"
LABEL_RUNTIME = "multiagent.poc.runtime"

DEFAULT_IMAGE = "multiagent-poc-agent:phase1"
WORKSPACE_PATH = "/workspace"
WORKSPACE_SIZE_MB = 16
AGENT_UID = 10001
RESULT_PREFIX = "@@AGENT_RESULT@@"
MAX_RESULT_BYTES = 256 * 1024
MAX_TASK_JSON_BYTES = 64 * 1024

TASK_TYPES = ("echo", "sleep", "compute", "fail")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ErrorKind(str, Enum):
    APP_FAILURE = "app_failure"  # the agent ran and reported / exhibited failure
    STARTUP_FAILURE = "startup_failure"  # container could not be created or started
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INFRASTRUCTURE = "infrastructure"  # docker daemon / image / orchestrator problem


class ValidationError(ValueError):
    pass


@dataclass
class TaskConfig:
    type: str
    params: dict = field(default_factory=dict)
    task_id: Optional[str] = None
    timeout_s: float = 30.0
    cpus: float = 0.5
    memory_mb: int = 256
    pids_limit: int = 64
    image: str = DEFAULT_IMAGE

    def __post_init__(self):
        if self.task_id is None:
            self.task_id = f"task-{uuid.uuid4().hex[:8]}"

    def validate(self) -> None:
        if self.type not in TASK_TYPES:
            raise ValidationError(f"unsupported task type {self.type!r}; expected one of {TASK_TYPES}")
        if not isinstance(self.params, dict):
            raise ValidationError("params must be a dict")
        if not _ID_RE.match(self.task_id or ""):
            raise ValidationError(f"invalid task_id {self.task_id!r}")
        if not 0 < self.timeout_s <= 3600:
            raise ValidationError("timeout_s must be in (0, 3600]")
        if not 0 < self.cpus <= 4:
            raise ValidationError("cpus must be in (0, 4]")
        if not 16 <= self.memory_mb <= 4096:
            raise ValidationError("memory_mb must be in [16, 4096]")
        if not 8 <= self.pids_limit <= 1024:
            raise ValidationError("pids_limit must be in [8, 1024]")
        if not self.image:
            raise ValidationError("image must be set")
        try:
            size = len(self.task_json().encode())
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"params are not JSON serialisable: {exc}")
        if size > MAX_TASK_JSON_BYTES:
            raise ValidationError(f"task JSON too large ({size} > {MAX_TASK_JSON_BYTES} bytes)")

    def task_json(self) -> str:
        return json.dumps({"task_id": self.task_id, "type": self.type, "params": self.params})


@dataclass
class ContainerSpec:
    """Everything the Docker layer needs to create one hardened agent container."""

    name: str
    image: str
    labels: dict
    env: dict
    cpus: float
    memory_mb: int
    pids_limit: int
    workspace_size_mb: int = WORKSPACE_SIZE_MB
    uid: int = AGENT_UID
    # Phase 2 extensions; the defaults reproduce the Phase 1 container exactly.
    workspace_tmpfs: bool = True  # Phase 1: tmpfs /workspace. Phase 2 tasks bind-mount their own dir instead.
    tmpfs: tuple = ()  # extra (container_path, mount_options) tmpfs mounts
    mounts: tuple = ()  # (host_path, container_path, read_only) bind mounts; only explicit paths, never broad ones
    args: tuple = ()  # arguments passed to the image entrypoint
    user: Optional[str] = None  # default "<uid>:<uid>"
    stop_timeout_s: int = 2
    log_max_size: str = "1m"
    entrypoint: Optional[str] = None  # set only by runtime adapters (e.g. diagnostics); never by task requests


@dataclass
class AgentResult:
    task_id: str
    agent_id: str
    status: str  # completed | failed | timed_out | cancelled
    exit_code: Optional[int]
    duration_ms: Optional[int]
    output: Optional[dict]
    error: Optional[dict]  # {"kind": ErrorKind value, "message": str, ...}
    container_id: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "output": self.output,
            "error": self.error,
            "container_id": self.container_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class AgentRecord:
    """In-memory registry entry for one agent (Phase 1 has no persistence)."""

    agent_id: str
    config: TaskConfig
    container_name: str
    container_id: Optional[str] = None
    sm: StateMachine = field(default_factory=StateMachine)
    result: Optional[AgentResult] = None
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    timed_out: bool = False
    started_monotonic: Optional[float] = None
    deadline: Optional[float] = None
    slot_held: bool = False
    # Serialises state transitions / finalisation; waiting on a container never holds it.
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def state(self) -> AgentState:
        return self.sm.state
