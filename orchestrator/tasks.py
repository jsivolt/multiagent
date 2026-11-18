"""Phase 2 task layer: runs a real coding-agent CLI in a hardened container and tracks it to completion.

Builds on Phase 1 (Orchestrator for run id / lease / docker client / stale sweep, StateMachine, RunLease,
build_create_args hardening). All task state lives on disk under `<workspace_root>/.tasks/<task_id>/`
(never mounted into any container), which is what makes results survive container removal and lets a restarted
orchestrator reconcile still-running containers.

    <workspace_root>/
        <workspace_id>/          bind-mounted at /workspace in exactly one task container (agent-writable)
        .tasks/<task_id>/        task.json events.jsonl stdout.log stderr.log result.json changes.diff
                                 baseline/ runtime/ (mounted ro at /run/task) secrets/ (transient, ro)
        .locks/                  per-workspace and per-task flock files
        .runs/                   Phase 1 run leases
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from . import workspace as ws
from .docker_client import DockerClient, DockerError
from .lifecycle import TERMINAL_STATES, AgentState, InvalidTransition, LeaseHeldError, RunLease, StateMachine
from .llm_relay import DEFAULT_UPSTREAM, LLMRelay
from .manager import Orchestrator
from .models import (
    KIND_TASK,
    LABEL_AGENT_ID,
    LABEL_CREATED_AT,
    LABEL_KIND,
    LABEL_PROJECT,
    LABEL_PROJECT_VALUE,
    LABEL_RUNTIME,
    LABEL_RUN_ID,
    LABEL_TASK_ID,
    ContainerSpec,
    ValidationError,
)
from .runtimes import get_runtime
from .runtimes.base import LaunchPlan

log = logging.getLogger("orchestrator.tasks")

LOG_LINE_MAX = 256 * 1024
LOG_FILE_MAX = 20 * 1024 * 1024
MAX_EVENTS = 20000
MAX_PROMPT_BYTES = 64 * 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,127}$")
_CRED_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_LIVE = {"running", "restarting", "paused", "created"}


class TaskNotFound(KeyError):
    pass


class TaskConflict(RuntimeError):
    """task_id already exists with a different request."""


class WorkspaceBusy(RuntimeError):
    pass


class TaskNotFinished(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _mem_mb(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    m = re.fullmatch(r"(\d+)\s*([kmg]?)b?", str(value).strip().lower())
    if not m:
        raise ValidationError(f"invalid memory value {value!r} (use e.g. 4g, 512m)")
    return int(m.group(1)) * {"": 1, "k": 1 / 1024, "m": 1, "g": 1024}[m.group(2)]


# ----------------------------------------------------------------------------------- request
_ALIASES = {
    "taskId": "task_id", "timeoutSeconds": "timeout_s", "memory": "memory_mb", "memoryMb": "memory_mb",
    "pids": "pids_limit", "pidsLimit": "pids_limit", "workspaceQuotaMb": "workspace_quota_mb",
}


@dataclass
class TaskRequest:
    prompt: str
    runtime: str = "pi"
    task_id: Optional[str] = None
    workspace: Optional[str] = None  # logical workspace id (not a path); defaults to the task id
    timeout_s: float = 900.0
    provider: str = "ollama"
    model: Optional[str] = None
    image: Optional[str] = None
    memory_mb: int = 2048
    cpus: float = 2.0
    pids_limit: int = 512
    network: str = "none"
    workspace_quota_mb: int = 256
    credentials: list = field(default_factory=list)  # NAMES of orchestrator env vars to inject; values are never stored
    options: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRequest":
        if not isinstance(data, dict):
            raise ValidationError("task request must be an object")
        flat = {**(data.get("agent") or {}), **(data.get("container") or {}),
                **{k: v for k, v in data.items() if k not in ("agent", "container")}}
        out = {}
        for key, value in flat.items():
            key = _ALIASES.get(key, key)
            if key not in cls.__dataclass_fields__:
                raise ValidationError(f"unknown task field {key!r}")
            out[key] = value
        if "prompt" not in out:
            raise ValidationError("prompt is required")
        if "memory_mb" in out:
            out["memory_mb"] = _mem_mb(out["memory_mb"])
        try:
            for key, conv in (("timeout_s", float), ("cpus", float), ("pids_limit", int), ("workspace_quota_mb", int)):
                if key in out:
                    out[key] = conv(out[key])
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"invalid numeric field: {exc}")
        return cls(**out)

    def validate(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValidationError("prompt must be a non-empty string")
        if len(self.prompt.encode()) > MAX_PROMPT_BYTES or "\0" in self.prompt:
            raise ValidationError("prompt too large or contains NUL")
        try:
            get_runtime(self.runtime)
        except KeyError as exc:
            raise ValidationError(str(exc.args[0]))
        try:
            ws.validate_id(self.task_id, "task_id")
            ws.validate_id(self.workspace, "workspace")
        except ws.WorkspaceError as exc:
            raise ValidationError(str(exc))
        for what, value in (("provider", self.provider), ("model", self.model), ("image", self.image)):
            if value is not None and not _NAME_RE.match(value):
                raise ValidationError(f"invalid {what} {value!r}")
        if not 0 < self.timeout_s <= 86400:
            raise ValidationError("timeoutSeconds must be in (0, 86400]")
        if not 0 < self.cpus <= 16:
            raise ValidationError("cpus must be in (0, 16]")
        if not 256 <= self.memory_mb <= 32768:
            raise ValidationError("memory must be between 256m and 32g")
        if not 16 <= self.pids_limit <= 4096:
            raise ValidationError("pids must be in [16, 4096]")
        if not 1 <= self.workspace_quota_mb <= 4096:
            raise ValidationError("workspaceQuotaMb must be in [1, 4096]")
        if self.network != "none":
            raise ValidationError(f"network mode {self.network!r} is not implemented; only 'none' is supported")
        if not isinstance(self.credentials, list) or not all(isinstance(c, str) and _CRED_RE.match(c) for c in self.credentials):
            raise ValidationError("credentials must be a list of environment variable NAMES like OPENAI_API_KEY")
        if not isinstance(self.options, dict):
            raise ValidationError("options must be an object")
        try:
            json.dumps(self.options)
        except (TypeError, ValueError):
            raise ValidationError("options must be JSON serialisable")

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


# ----------------------------------------------------------------------------------- redaction
_SECRET_PATTERNS = [re.compile(p) for p in (
    r"\bsk-[A-Za-z0-9_-]{16,}", r"Bearer\s+[A-Za-z0-9._~+/-]{16,}", r"gh[pousr]_[A-Za-z0-9]{20,}", r"AKIA[0-9A-Z]{16}",
)]


class Redactor:
    def __init__(self, values=()):
        self.values = sorted({v for v in values if v and len(v) >= 6}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for v in self.values:
            if v in text:
                text = text.replace(v, "[REDACTED]")
        for pat in _SECRET_PATTERNS:
            text = pat.sub("[REDACTED]", text)
        return text


# ----------------------------------------------------------------------------------- live task
class _Stop(Exception):
    def __init__(self, state, kind, code, message, exit_code=None):
        self.state, self.kind, self.code, self.message, self.exit_code = state, kind, code, message, exit_code


@dataclass
class _Live:
    rec: dict  # persisted task.json content
    request: TaskRequest
    task_dir: Path
    sm: StateMachine = field(default_factory=StateMachine)
    lock: threading.RLock = field(default_factory=threading.RLock)
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    parser: object = None
    runtime: object = None
    plan: Optional[LaunchPlan] = None
    redact: Redactor = field(default_factory=Redactor)
    relay: Optional[LLMRelay] = None
    relay_tmp: Optional[Path] = None
    locks: list = field(default_factory=list)
    slot_held: bool = False
    stop_reason: Optional[str] = None
    seq: int = 0
    events_dropped: int = 0
    log_bytes: dict = field(default_factory=lambda: {"stdout": 0, "stderr": 0})
    log_idx: dict = field(default_factory=lambda: {"stdout": 0, "stderr": 0})
    skip: dict = field(default_factory=lambda: {"stdout": 0, "stderr": 0})
    log_fh: dict = field(default_factory=dict)
    ev_lock: threading.Lock = field(default_factory=threading.Lock)
    parse_lock: threading.Lock = field(default_factory=threading.Lock)
    ws_dir: Optional[Path] = None
    adopted: bool = False


class TaskManager:
    def __init__(
        self,
        *,
        orchestrator: Optional[Orchestrator] = None,
        docker: Optional[DockerClient] = None,
        workspace_root="workspaces",
        max_concurrency: int = 2,
        poll_interval_s: float = 0.25,
        term_grace_s: int = 3,
        upstream=DEFAULT_UPSTREAM,
    ):
        self.orch = orchestrator or Orchestrator(docker=docker, workspace_root=workspace_root,
                                                 max_concurrency=max_concurrency)
        self._owns_orch = orchestrator is None
        self.docker = self.orch.docker
        self.run_id = self.orch.run_id
        self.root = self.orch.workspace_root
        self.tasks_dir = self.root / ".tasks"
        self.locks_dir = self.root / ".locks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.poll_interval_s = poll_interval_s
        self.term_grace_s = term_grace_s
        self.upstream = upstream
        self.uid, self.gid = (os.getuid() or 10001), (os.getgid() or 10001)
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._live: dict[str, _Live] = {}
        self._registry_lock = threading.Lock()
        self._closing = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ persistence
    def _task_dir(self, task_id: str) -> Path:
        return self.tasks_dir / ws.validate_id(task_id, "task_id")

    def _read(self, task_id: str) -> dict:
        try:
            return json.loads((self._task_dir(task_id) / "task.json").read_text())
        except FileNotFoundError:
            raise TaskNotFound(task_id) from None

    @staticmethod
    def _write_json(path: Path, data) -> None:
        tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)

    def _save(self, live: _Live) -> None:
        with live.lock:
            live.rec["state"] = live.sm.state.value
            live.rec["stateHistory"] = [(s.value, t) for s, t in live.sm.history]
            live.rec["logLines"] = dict(live.log_idx)
            live.rec["updatedAt"] = _now()
            self._write_json(live.task_dir / "task.json", live.rec)

    def _emit(self, live: _Live, etype: str, **data) -> None:
        with live.ev_lock:
            if live.seq >= MAX_EVENTS and etype in ("agent.output",):
                live.events_dropped += 1
                return
            live.seq += 1
            event = {"seq": live.seq, "taskId": live.rec["taskId"], "timestamp": _now(), "type": etype,
                     "runtime": live.rec["runtime"], "data": json.loads(live.redact(json.dumps(data, default=str)))}
            with open(live.task_dir / "events.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")

    def _transition(self, live: _Live, state: AgentState) -> None:
        with live.lock:
            live.sm.transition(state)
            self._save(live)

    # ------------------------------------------------------------------ public API
    def submit_task(self, request) -> dict:
        """Validate, register and start a task asynchronously. Re-submitting an identical request is a no-op."""
        req = request if isinstance(request, TaskRequest) else TaskRequest.from_dict(request)
        req.task_id = req.task_id or f"task-{uuid.uuid4().hex[:8]}"
        req.workspace = req.workspace or req.task_id
        req.validate()
        runtime = get_runtime(req.runtime)
        req.model = req.model or getattr(runtime, "default_model", None)
        plan = runtime.prepare(req)  # runtime-specific option validation happens before anything is created
        with self._registry_lock:
            if self._closing:
                raise RuntimeError("task manager is shutting down")
            task_dir = self._task_dir(req.task_id)
            try:
                task_dir.mkdir(mode=0o700)  # atomic: only one submitter can create a given task id
            except FileExistsError:
                existing = self._read(req.task_id)
                if existing.get("fingerprint") == req.fingerprint():
                    return self.get_task(req.task_id)
                raise TaskConflict(f"task {req.task_id!r} already exists with a different request") from None
            locks = []
            try:
                locks.append(RunLease.acquire(self.locks_dir, f"task-{req.task_id}"))
                locks.append(RunLease.acquire(self.locks_dir, f"ws-{req.workspace}"))
            except LeaseHeldError as exc:
                for lock in locks:
                    lock.release()
                ws.remove_tree(task_dir)
                raise WorkspaceBusy(f"workspace {req.workspace!r} is in use by another active task") from exc
            live = _Live(rec={}, request=req, task_dir=task_dir, runtime=runtime, plan=plan, locks=locks,
                         parser=runtime.new_parser())
            live.rec = {
                "taskId": req.task_id, "runtime": req.runtime, "workspace": req.workspace, "runId": self.run_id,
                "request": asdict(req), "fingerprint": req.fingerprint(), "state": "CREATED",
                "createdAt": _now(), "containerId": None, "containerName": f"poc-task-{self.run_id}-{req.task_id}",
                "startedAt": None, "finishedAt": None, "deadlineEpoch": None, "relaySocketDir": None,
                "allowedModels": list(plan.allowed_models),
            }
            self._live[req.task_id] = live
            self._save(live)
        self._emit(live, "task.created", status="CREATED", workspace=req.workspace, model=req.model, provider=req.provider)
        live.thread = threading.Thread(target=self._run, args=(live,), name=f"task-{req.task_id}", daemon=True)
        live.thread.start()
        return self.get_task(req.task_id)

    def get_task(self, task_id: str) -> dict:
        rec = self._read(task_id)
        result = self._read_result(task_id)
        return {**{k: rec.get(k) for k in ("taskId", "runtime", "state", "workspace", "containerId", "createdAt",
                                          "startedAt", "finishedAt", "runId", "updatedAt")},
                "model": rec["request"].get("model"), "provider": rec["request"].get("provider"),
                "status": result["status"] if result else None, "result": result}

    def get_task_events(self, task_id: str, since_seq: int = 0) -> list:
        self._read(task_id)
        events = []
        try:
            with open(self._task_dir(task_id) / "events.jsonl", encoding="utf-8") as fh:
                for line in fh:
                    ev = json.loads(line)
                    if ev["seq"] > since_seq:
                        events.append(ev)
        except FileNotFoundError:
            pass
        return events

    def stream_events(self, task_id: str, poll_s: float = 0.3) -> Iterator[dict]:
        """Yield events as they are recorded until the task is terminal and fully drained."""
        seq = 0
        while True:
            for ev in self.get_task_events(task_id, seq):
                seq = ev["seq"]
                yield ev
            if self._read(task_id)["state"] in ("REMOVED",) and not self.get_task_events(task_id, seq):
                return
            time.sleep(poll_s)

    def cancel_task(self, task_id: str) -> dict:
        """Cooperative cancel (SIGTERM, then SIGKILL after the grace period). Idempotent."""
        live = self._live.get(task_id)
        if live and not live.done.is_set():
            live.cancel.set()
        else:
            rec = self._read(task_id)
            if AgentState(rec["state"]) not in TERMINAL_STATES | {AgentState.CLEANUP, AgentState.REMOVED}:
                (self._task_dir(task_id) / "cancel.request").write_text(_now())  # picked up by the owning process
        return self.get_task(task_id)

    def get_task_result(self, task_id: str) -> dict:
        result = self._read_result(task_id)
        if result is None:
            self._read(task_id)
            raise TaskNotFinished(task_id)
        return result

    def list_tasks(self) -> list:
        out = []
        for d in sorted(self.tasks_dir.iterdir()):
            try:
                out.append(self.get_task(d.name))
            except (TaskNotFound, ValueError):
                continue
        return out

    def wait(self, task_id: str, timeout: Optional[float] = None) -> dict:
        deadline = None if timeout is None else time.monotonic() + timeout
        live = self._live.get(task_id)
        if live:
            live.done.wait(None if deadline is None else max(0, deadline - time.monotonic()))
        else:
            while self._read(task_id)["state"] != "REMOVED":
                if deadline is not None and time.monotonic() > deadline:
                    break
                time.sleep(0.2)
        return self.get_task(task_id)

    def cleanup_workspace(self, workspace_id: str) -> bool:
        """Delete a task workspace (separate from container removal). False if a task is still using it."""
        ws.validate_id(workspace_id, "workspace")
        try:
            lock = RunLease.acquire(self.locks_dir, f"ws-{workspace_id}")
        except LeaseHeldError:
            return False
        try:
            ws.remove_tree(self.root / workspace_id)
        finally:
            lock.release()
        return True

    def purge_task(self, task_id: str) -> None:
        """Delete the stored record (events/logs/result/diff) of a finished task."""
        rec = self._read(task_id)
        if rec["state"] != "REMOVED":
            raise TaskNotFinished(task_id)
        ws.remove_tree(self._task_dir(task_id))
        self._live.pop(task_id, None)

    def close(self) -> None:
        """Cancel and clean up every task of this manager, then release the run lease."""
        with self._registry_lock:
            self._closing = True
        for live in list(self._live.values()):
            live.cancel.set()
        for live in list(self._live.values()):
            if live.thread:
                live.thread.join(timeout=60)
        if self._owns_orch:
            self.orch.close()

    # ------------------------------------------------------------------ worker
    def _read_result(self, task_id: str) -> Optional[dict]:
        try:
            return json.loads((self._task_dir(task_id) / "result.json").read_text())
        except FileNotFoundError:
            return None

    def _run(self, live: _Live, adopt: bool = False) -> None:
        try:
            try:
                if not adopt:
                    self._acquire_slot(live)
                    self._prepare(live)
                    self._start(live)
                self._watch(live)
            except _Stop as stop:
                self._finish(live, stop.state, stop.kind, stop.code, stop.message, stop.exit_code)
            except Exception as exc:  # never leave a task without a terminal state
                log.exception("task %s crashed", live.rec["taskId"])
                self._finish(live, AgentState.FAILED, "infrastructure", "internal_error", repr(exc))
        finally:
            self._cleanup(live)
            live.done.set()

    def _acquire_slot(self, live: _Live) -> None:
        while not self._slots.acquire(timeout=0.2):
            if live.cancel.is_set():
                raise _Stop(AgentState.CANCELLED, "cancelled", "cancelled", "cancelled before start")
        live.slot_held = True
        if live.cancel.is_set():
            raise _Stop(AgentState.CANCELLED, "cancelled", "cancelled", "cancelled before start")

    def _prepare(self, live: _Live) -> None:
        req, plan = live.request, live.plan
        self._transition(live, AgentState.PREPARING)
        self._emit(live, "task.preparing")
        try:
            if not self.docker.image_exists(plan.image):
                raise _Stop(AgentState.FAILED, "infrastructure", "image_missing",
                            f"image {plan.image!r} not found; build it with `python -m orchestrator build --runtime {req.runtime}`")
            if plan.required_image_uid_label:
                label = self.docker.image_label(plan.image, plan.required_image_uid_label)
                if label != str(self.uid):
                    raise _Stop(AgentState.FAILED, "infrastructure", "image_uid_mismatch",
                                f"image was built for uid {label}, orchestrator runs as uid {self.uid}; rebuild the image")
            live.ws_dir = ws.resolve_workspace(self.root, req.workspace)
            if os.getuid() == 0:
                os.chown(live.ws_dir, self.uid, self.gid)
            info = ws.snapshot_baseline(live.ws_dir, live.task_dir / "baseline")
            self._emit(live, "workspace.prepared", workspace=req.workspace, baselineFiles=info["files"])
        except (ws.WorkspaceError, OSError) as exc:
            raise _Stop(AgentState.FAILED, "infrastructure", "workspace_error", str(exc))
        runtime_dir = live.task_dir / "runtime"
        runtime_dir.mkdir(mode=0o755, exist_ok=True)
        os.chmod(runtime_dir, 0o755)
        for name, text in plan.files.items():
            (runtime_dir / name).write_text(text)
            os.chmod(runtime_dir / name, 0o644)
        secrets = {}
        for name in req.credentials:
            if name not in os.environ:
                raise _Stop(AgentState.FAILED, "startup_failure", "missing_credentials",
                            f"credential {name} is not set in the orchestrator environment")
            secrets[name] = os.environ[name]
        plan.secrets.update(secrets)
        live.redact = Redactor(plan.secrets.values())
        if plan.secrets:
            sdir = live.task_dir / "secrets"
            sdir.mkdir(mode=0o700, exist_ok=True)
            os.chmod(sdir, 0o755)  # directory must be traversable by the container uid; files are 0400 owner-only
            for name, value in plan.secrets.items():
                fd = os.open(sdir / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o400)
                with os.fdopen(fd, "w") as fh:
                    fh.write(value)
        if plan.needs_llm_relay:
            live.relay_tmp = Path(tempfile.mkdtemp(prefix="mapoc-"))  # short path: unix sockets are limited to ~100 chars
            live.rec["relaySocketDir"] = str(live.relay_tmp)
            live.relay = LLMRelay(live.relay_tmp, plan.allowed_models, upstream=self.upstream).start()

    def _spec(self, live: _Live) -> ContainerSpec:
        req, plan = live.request, live.plan
        mounts = [(str(live.ws_dir), "/workspace", False), (str(live.task_dir / "runtime"), "/run/task", True)]
        if plan.secrets:
            mounts.append((str(live.task_dir / "secrets"), "/run/secrets", True))
        env = dict(plan.env)
        if live.relay_tmp:
            mounts.append((str(live.relay_tmp), "/run/llm", True))
            env["LLM_SOCKET"] = "/run/llm/llm.sock"
        owned = f"uid={self.uid},gid={self.gid}"
        return ContainerSpec(
            name=live.rec["containerName"], image=plan.image, env=env, args=tuple(plan.args),
            labels={
                LABEL_PROJECT: LABEL_PROJECT_VALUE, LABEL_RUN_ID: self.run_id, LABEL_TASK_ID: req.task_id,
                LABEL_AGENT_ID: req.task_id, LABEL_KIND: KIND_TASK, LABEL_RUNTIME: req.runtime,
                LABEL_CREATED_AT: str(int(time.time())),
            },
            cpus=req.cpus, memory_mb=req.memory_mb, pids_limit=req.pids_limit, workspace_tmpfs=False,
            mounts=tuple(mounts), user=f"{self.uid}:{self.gid}", stop_timeout_s=self.term_grace_s, log_max_size="20m",
            entrypoint=plan.entrypoint,
            tmpfs=(("/tmp", "rw,noexec,nosuid,nodev,size=256m,mode=1777"),
                   ("/home/agent", f"rw,noexec,nosuid,nodev,size=128m,{owned},mode=0700")),
        )

    def _start(self, live: _Live) -> None:
        spec = self._spec(live)
        self._transition(live, AgentState.STARTING)
        self._emit(live, "container.starting", image=spec.image, containerName=spec.name)
        if live.cancel.is_set():
            raise _Stop(AgentState.CANCELLED, "cancelled", "cancelled", "cancelled before start")
        try:
            live.rec["containerId"] = self.docker.create_container(spec)
            self._save(live)
            self.docker.start_container(live.rec["containerId"])
        except DockerError as exc:
            kind = "infrastructure" if exc.unavailable else "startup_failure"
            raise _Stop(AgentState.FAILED, kind, "docker_unavailable" if exc.unavailable else "container_start_failed", str(exc))
        live.rec["startedAt"] = _now()
        live.rec["deadlineEpoch"] = time.time() + live.request.timeout_s
        self._transition(live, AgentState.RUNNING)
        self._emit(live, "container.started", containerId=live.rec["containerId"])
        self._emit(live, "agent.starting", model=live.request.model, provider=live.request.provider)

    # -- watching ---------------------------------------------------------------------
    def _on_line(self, live: _Live, stream: str, line: str, truncated: bool) -> None:
        with live.parse_lock:  # stdout and stderr readers share the parser and counters
            live.log_idx[stream] += 1
            replay = live.log_idx[stream] <= live.skip[stream]
            events = live.parser.feed(stream, line)
        if replay:
            return
        text = live.redact(line) + ("…[line truncated]" if truncated else "")
        if live.log_bytes[stream] < LOG_FILE_MAX:
            fh = live.log_fh.get(stream)
            if fh is None:
                fh = live.log_fh[stream] = open(live.task_dir / f"{stream}.log", "a", encoding="utf-8")
            fh.write(text + "\n")
            fh.flush()
            live.log_bytes[stream] += len(text) + 1
        for etype, data in events:
            self._emit(live, etype, **data)

    def _reader(self, live: _Live, stream: str, pipe) -> None:
        try:
            while True:
                raw = pipe.readline(LOG_LINE_MAX)
                if not raw:
                    return
                truncated = len(raw) == LOG_LINE_MAX and not raw.endswith(b"\n")
                if truncated:  # discard the rest of an oversized line
                    while True:
                        rest = pipe.readline(LOG_LINE_MAX)
                        if not rest or rest.endswith(b"\n"):
                            break
                self._on_line(live, stream, raw.decode("utf-8", "replace").rstrip("\r\n"), truncated)
        except (OSError, ValueError):
            return

    def _inspect(self, live: _Live) -> Optional[dict]:
        failures = 0
        while True:
            try:
                return self.docker.inspect_container(live.rec["containerId"])
            except DockerError as exc:
                if exc.not_found:
                    return None
                failures += 1
                if failures >= 3:
                    raise _Stop(AgentState.FAILED, "infrastructure", "docker_unavailable", f"lost contact with docker: {exc}")
                time.sleep(self.poll_interval_s)

    def _stop_container(self, live: _Live, reason: str) -> None:
        if live.stop_reason is None:
            live.stop_reason = reason
            self._emit(live, "agent.stopping", reason=reason, graceSeconds=self.term_grace_s)
        try:
            self.docker.stop_container(live.rec["containerId"], self.term_grace_s)  # SIGTERM, then SIGKILL
            self.docker.kill_container(live.rec["containerId"])
        except DockerError as exc:
            log.warning("stop failed for %s: %s", live.rec["taskId"], exc)

    def _watch(self, live: _Live) -> None:
        cid = live.rec["containerId"]
        req = live.request
        proc = self.docker.follow_logs(cid)
        readers = [threading.Thread(target=self._reader, args=(live, s, p), daemon=True)
                   for s, p in (("stdout", proc.stdout), ("stderr", proc.stderr))]
        for r in readers:
            r.start()
        quota = req.workspace_quota_mb * 1024 * 1024
        last_save = last_quota = time.monotonic()
        try:
            while True:
                info = self._inspect(live)
                if info is None:
                    if live.cancel.is_set():
                        raise _Stop(AgentState.CANCELLED, "cancelled", "cancelled", "cancelled")
                    raise _Stop(AgentState.FAILED, "infrastructure", "container_lost", "container disappeared while running")
                if info["State"].get("Status") not in _LIVE:
                    break
                now = time.monotonic()
                if live.stop_reason is None:
                    if live.cancel.is_set() or (live.task_dir / "cancel.request").exists():
                        self._stop_container(live, "cancelled")
                    elif time.time() >= live.rec["deadlineEpoch"]:
                        self._stop_container(live, "timeout")
                    elif now - last_quota > 2:
                        last_quota = now
                        if live.ws_dir and ws.tree_size(live.ws_dir, quota) > quota:
                            self._stop_container(live, "quota")
                    if live.stop_reason:
                        continue
                if now - last_save > 2:
                    last_save = now
                    self._save(live)
                time.sleep(self.poll_interval_s)
        finally:
            if proc.poll() is None:
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
            for r in readers:
                r.join(timeout=10)
        self._classify_and_finish(live, info)

    def _classify_and_finish(self, live: _Live, info: dict) -> None:
        st = info["State"]
        code = st.get("ExitCode")
        failure = live.parser.classify(code)
        relay = live.relay
        if live.stop_reason and failure is not None:
            if live.stop_reason == "timeout":
                raise _Stop(AgentState.TIMED_OUT, "timeout", "timeout",
                            f"exceeded timeout of {live.request.timeout_s:g}s; agent stopped", code)
            if live.stop_reason == "cancelled":
                raise _Stop(AgentState.CANCELLED, "cancelled", "cancelled", "cancelled by orchestrator", code)
            raise _Stop(AgentState.FAILED, "agent_failure", "workspace_quota_exceeded",
                        f"workspace exceeded {live.request.workspace_quota_mb} MB quota; agent stopped", code)
        if failure is None:
            return self._finish(live, AgentState.COMPLETED, None, None, None, code)
        if st.get("OOMKilled"):
            raise _Stop(AgentState.FAILED, "agent_failure", "out_of_memory", "agent killed: out of memory (OOMKilled)", code)
        if relay and relay.unreachable and failure.code in ("provider_unreachable", "agent_error", "no_result", "model_error"):
            raise _Stop(AgentState.FAILED, "infrastructure", "llm_backend_unreachable",
                        f"model backend unreachable via relay: {relay.unreachable[-1]}", code)
        if st.get("Error") or (code in (126, 127) and not live.parser.started):
            raise _Stop(AgentState.FAILED, "startup_failure", "runtime_start_failed",
                        f"container could not run the agent runtime: {st.get('Error') or failure.message}", code)
        if relay and relay.denied and failure.code == "agent_error":
            failure.message += f" (relay denied: {relay.denied[-1]})"
        raise _Stop(AgentState.FAILED, "agent_failure", failure.code, failure.message, code)

    # -- finishing --------------------------------------------------------------------
    def _finish(self, live: _Live, state: AgentState, kind, code, message, exit_code=None) -> None:
        """Collect results, persist the terminal record (preserved even after the container is removed)."""
        with live.lock:
            if live.sm.is_terminal or live.sm.state in (AgentState.CLEANUP, AgentState.REMOVED):
                return
            cid = live.rec.get("containerId")
            if cid and state in (AgentState.CANCELLED, AgentState.TIMED_OUT, AgentState.FAILED):
                try:  # make sure nothing keeps running while we collect the workspace
                    self.docker.kill_container(cid)
                except DockerError:
                    pass
            changes = {"artifacts": [], "counts": {}, "diff": "", "diffTruncated": False, "scanTruncated": False}
            if live.ws_dir and (live.task_dir / "baseline").exists():
                try:
                    changes = ws.collect_changes(live.ws_dir, live.task_dir / "baseline")
                except OSError as exc:
                    log.warning("collecting changes failed for %s: %s", live.rec["taskId"], exc)
            diff_file = None
            if changes["diff"]:
                diff_file = str(live.task_dir / "changes.diff")
                Path(diff_file).write_text(live.redact(changes["diff"]))
            finished = _now()
            live.rec["finishedAt"] = finished
            started = live.rec.get("startedAt")
            duration = None
            if started:
                duration = int((datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds() * 1000)
            error = None
            if kind:
                error = {"kind": kind, "code": code, "message": live.redact(message or "")}
            result = {
                "taskId": live.rec["taskId"], "containerId": live.rec.get("containerId"), "runtime": live.rec["runtime"],
                "status": state.value.lower(), "exitCode": exit_code, "startedAt": started, "finishedAt": finished,
                "durationMs": duration, "workspace": live.rec["workspace"],
                "workspacePath": str(live.ws_dir) if live.ws_dir else None,
                "model": live.request.model, "provider": live.request.provider,
                "artifacts": changes["artifacts"],
                "changes": {**changes["counts"], "diffFile": diff_file, "diffTruncated": changes["diffTruncated"],
                            "scanTruncated": changes["scanTruncated"]},
                "output": live.parser.summary(),
                "logs": {"stdout": str(live.task_dir / "stdout.log"), "stderr": str(live.task_dir / "stderr.log"),
                         "events": str(live.task_dir / "events.jsonl")},
                "error": error,
            }
            if live.relay:
                result["modelRelay"] = {"requests": live.relay.requests, "denied": len(live.relay.denied),
                                        "upstreamErrors": len(live.relay.upstream_errors)}
            self._write_json(live.task_dir / "result.json", result)
            live.sm.transition(state)
            self._save(live)
        if state is AgentState.COMPLETED:
            self._emit(live, "agent.completed", exitCode=exit_code)
            self._emit(live, "task.completed", status="completed", artifacts=len(result["artifacts"]))
        elif state is AgentState.CANCELLED:
            self._emit(live, "task.cancelled", status="cancelled")
        else:
            self._emit(live, "agent.failed", exitCode=exit_code, error=error)
            self._emit(live, "task.failed", status=state.value.lower(), error=error)

    def _cleanup(self, live: _Live) -> None:
        """Container removal, transient credentials, relay and locks. Idempotent; the result is already preserved."""
        rec = live.rec
        try:
            if live.sm.state in TERMINAL_STATES:
                self._transition(live, AgentState.CLEANUP)
            cid = rec.get("containerId")
            removed = True
            if cid:
                for attempt in range(3):
                    try:
                        self.docker.remove_container(cid)
                        break
                    except DockerError as exc:
                        removed = False
                        log.warning("remove %s failed (attempt %d): %s", cid[:12], attempt + 1, exc)
                        time.sleep(0.5)
                else:
                    removed = False
                    return  # stays in CLEANUP; reconcile() retries
                removed = True
            if removed and live.sm.state is AgentState.CLEANUP:
                self._transition(live, AgentState.REMOVED)
                self._emit(live, "container.cleaned", containerId=cid)
        except InvalidTransition:
            pass
        finally:
            ws.remove_tree(live.task_dir / "secrets")  # task-scoped credential material never outlives the run
            if live.relay:
                live.relay.stop()
            if live.relay_tmp:
                ws.remove_tree(live.relay_tmp)
            for fh in live.log_fh.values():
                fh.close()
            if live.slot_held:
                live.slot_held = False
                self._slots.release()
            for lock in live.locks:
                lock.release()
            live.locks = []

    # ------------------------------------------------------------------ restart reconciliation
    def reconcile(self) -> dict:
        """Adopt or finalise task containers left by dead orchestrator runs. Touches only containers that carry
        this POC's labels, kind=agent-task, a task record on disk, and whose owning run's lease is free."""
        report = {"adopted": [], "removed": [], "skippedActiveRuns": [], "failed": [], "ignored": []}
        for c in self.docker.list_containers({LABEL_PROJECT: LABEL_PROJECT_VALUE, LABEL_KIND: KIND_TASK}):
            labels = c["Config"].get("Labels") or {}
            task_id, run_id = labels.get(LABEL_TASK_ID), labels.get(LABEL_RUN_ID)
            if run_id == self.run_id or task_id in self._live:
                continue
            if not run_id or not task_id or RunLease.is_active(self.orch.state_dir, run_id):
                (report["skippedActiveRuns"] if run_id else report["ignored"]).append(c["Id"][:12])
                continue
            try:
                rec = self._read(task_id)
            except (TaskNotFound, ValueError):
                self._safe_remove(c["Id"], report)  # owned, stale, no record: nothing can adopt it
                continue
            if rec.get("containerId") != c["Id"]:
                report["ignored"].append(c["Id"][:12])
                continue
            try:
                self._adopt(rec, c, report)
            except LeaseHeldError:
                report["skippedActiveRuns"].append(c["Id"][:12])
        # Tasks that were mid-flight but whose container vanished.
        for rec_path in self.tasks_dir.glob("*/task.json"):
            try:
                rec = json.loads(rec_path.read_text())
            except (OSError, ValueError):
                continue
            tid = rec.get("taskId")
            if tid in self._live or rec.get("state") in ("REMOVED",) or rec.get("runId") == self.run_id:
                continue
            if RunLease.is_active(self.orch.state_dir, rec.get("runId", "")) or RunLease.is_active(self.locks_dir, f"task-{tid}"):
                continue
            if rec.get("state") in ("CREATED", "PREPARING", "STARTING", "RUNNING") and not (rec_path.parent / "result.json").exists():
                self._fail_orphaned_record(rec, "container lost before the orchestrator restarted", report)
        return report

    def _safe_remove(self, container_id: str, report: dict) -> None:
        try:
            self.docker.remove_container(container_id)
            report["removed"].append(container_id[:12])
        except DockerError as exc:
            log.warning("could not remove %s: %s", container_id[:12], exc)

    def _fail_orphaned_record(self, rec: dict, message: str, report: dict) -> None:
        req = TaskRequest(**rec["request"])
        live = _Live(rec=rec, request=req, task_dir=self._task_dir(rec["taskId"]), runtime=get_runtime(rec["runtime"]))
        live.parser = live.runtime.new_parser()
        live.locks = [RunLease.acquire(self.locks_dir, f"task-{rec['taskId']}")]
        try:
            self._fast_forward(live, AgentState(rec["state"]))
            self._finish(live, AgentState.FAILED, "infrastructure", "orchestrator_lost", message)
            self._cleanup(live)
            report["failed"].append(rec["taskId"])
        finally:
            for lock in live.locks:
                lock.release()

    @staticmethod
    def _fast_forward(live: _Live, state: AgentState) -> None:
        path = {AgentState.CREATED: [], AgentState.PREPARING: [AgentState.PREPARING],
                AgentState.STARTING: [AgentState.STARTING],
                AgentState.RUNNING: [AgentState.STARTING, AgentState.RUNNING]}.get(state, [])
        for step in path:
            live.sm.transition(step)

    def _adopt(self, rec: dict, container: dict, report: dict) -> None:
        task_id = rec["taskId"]
        task_dir = self._task_dir(task_id)
        req = TaskRequest(**rec["request"])
        status = container["State"]["Status"]
        recorded = AgentState(rec["state"])
        terminal_done = (task_dir / "result.json").exists()
        locks = [RunLease.acquire(self.locks_dir, f"task-{task_id}")]  # LeaseHeldError: someone else owns it
        try:
            locks.append(RunLease.acquire(self.locks_dir, f"ws-{rec['workspace']}"))
        except LeaseHeldError:
            locks[0].release()
            raise
        runtime = get_runtime(rec["runtime"])
        live = _Live(rec=rec, request=req, task_dir=task_dir, runtime=runtime, locks=locks, adopted=True,
                     parser=runtime.new_parser())
        live.plan = runtime.prepare(req)
        live.ws_dir = self.root / rec["workspace"]
        live.skip = dict(rec.get("logLines") or {"stdout": 0, "stderr": 0})
        live.log_idx = {"stdout": 0, "stderr": 0}
        events = self.get_task_events(task_id)
        live.seq = events[-1]["seq"] if events else 0
        live.rec["runId"] = self.run_id
        sdir = task_dir / "secrets"
        if sdir.is_dir():
            live.redact = Redactor(p.read_text() for p in sdir.iterdir() if p.is_file())
        if terminal_done or recorded in TERMINAL_STATES or recorded in (AgentState.CLEANUP, AgentState.REMOVED):
            live.sm = StateMachine()  # result already preserved: only the container is left to remove
            self._fast_forward(live, AgentState.RUNNING)
            live.sm.transition(AgentState(json.loads((task_dir / "result.json").read_text())["status"].upper())
                               if terminal_done else AgentState.FAILED)
            self._register_and_cleanup(live, report, "removed")
            return
        if status == "created" or recorded in (AgentState.CREATED, AgentState.PREPARING, AgentState.STARTING) and status != "running":
            self._fast_forward(live, AgentState.STARTING)
            self._register(live)
            self._finish(live, AgentState.FAILED, "infrastructure", "orchestrator_lost",
                         "orchestrator stopped before the agent started")
            self._cleanup(live)
            report["failed"].append(task_id)
            return
        # RUNNING (or already exited while we were down): resume watching with the original deadline.
        self._fast_forward(live, AgentState.RUNNING)
        live.cancel.clear()
        if not live.rec.get("deadlineEpoch"):
            live.rec["deadlineEpoch"] = time.time() + req.timeout_s
        if (task_dir / "cancel.request").exists():
            live.cancel.set()
        if live.plan.needs_llm_relay and rec.get("relaySocketDir") and status == "running":
            live.relay_tmp = Path(rec["relaySocketDir"])
            live.relay = LLMRelay(live.relay_tmp, rec.get("allowedModels") or live.plan.allowed_models,
                                  upstream=self.upstream).start()
        live.rec["adoptedAt"] = _now()
        self._register(live)
        self._emit(live, "task.reconciled", fromRun=container["Config"]["Labels"].get(LABEL_RUN_ID),
                   containerStatus=status)
        live.thread = threading.Thread(target=self._run, args=(live, True), name=f"adopt-{task_id}", daemon=True)
        live.thread.start()
        report["adopted"].append(task_id)

    def _register(self, live: _Live) -> None:
        with self._registry_lock:
            self._live[live.rec["taskId"]] = live
        self._save(live)

    def _register_and_cleanup(self, live: _Live, report: dict, bucket: str) -> None:
        self._register(live)
        self._cleanup(live)
        report[bucket].append(live.rec["taskId"])
        live.done.set()
