"""Orchestrator: dynamic creation, execution, monitoring and destruction of agent containers."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .docker_client import DockerClient, DockerError
from .lifecycle import (
    TERMINAL_STATES,
    AgentState,
    InvalidTransition,
    RunLease,
)
from .models import (
    LABEL_AGENT_ID,
    KIND_TASK,
    LABEL_CREATED_AT,
    LABEL_KIND,
    LABEL_PROJECT,
    LABEL_PROJECT_VALUE,
    LABEL_RUN_ID,
    LABEL_TASK_ID,
    MAX_RESULT_BYTES,
    RESULT_PREFIX,
    AgentRecord,
    AgentResult,
    ContainerSpec,
    ErrorKind,
    TaskConfig,
)

log = logging.getLogger("orchestrator")

__all__ = ["Orchestrator", "UnknownAgentError", "OrchestratorClosed"]

_PROJECT_FILTER = {LABEL_PROJECT: LABEL_PROJECT_VALUE}
_LIVE_DOCKER_STATES = {"running", "restarting", "paused", "created"}


class UnknownAgentError(KeyError):
    pass


class OrchestratorClosed(RuntimeError):
    pass


def _parse_docker_time(value: Optional[str]) -> Optional[datetime]:
    if not value or value.startswith("0001-01-01"):
        return None
    value = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds") if dt else None


class Orchestrator:
    """Runs outside the agent containers; the only component allowed to manage them.

    Concurrency: `max_concurrency` bounds how many containers are started/running at
    once. A slot is taken in `start_agent` and returned when the agent reaches a
    terminal state (or is destroyed).
    """

    def __init__(
        self,
        *,
        docker: Optional[DockerClient] = None,
        workspace_root: str | os.PathLike = "workspaces",
        max_concurrency: int = 4,
        cleanup_stale_on_start: bool = False,
        poll_interval_s: float = 0.1,
        run_id: Optional[str] = None,
    ):
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.docker = docker or DockerClient()
        self.workspace_root = Path(workspace_root).resolve()
        self.state_dir = self.workspace_root / ".runs"
        self.max_concurrency = max_concurrency
        self.poll_interval_s = poll_interval_s
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._agents: dict[str, AgentRecord] = {}
        self._registry_lock = threading.Lock()
        self._closing = False
        # Claim the run before creating any container so other orchestrators see us as active.
        self._lease = RunLease.acquire(self.state_dir, self.run_id)
        if cleanup_stale_on_start:
            self.cleanup_stale()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ registry
    def _get(self, agent_id: str) -> AgentRecord:
        try:
            return self._agents[agent_id]
        except KeyError:
            raise UnknownAgentError(agent_id) from None

    def _new_record(self, config: TaskConfig) -> AgentRecord:
        with self._registry_lock:
            if self._closing:
                raise OrchestratorClosed("orchestrator is shutting down")
            while True:
                agent_id = f"agent-{uuid.uuid4().hex[:8]}"
                if agent_id not in self._agents:
                    break
            rec = AgentRecord(
                agent_id=agent_id,
                config=config,
                container_name=f"poc-{self.run_id}-{agent_id}",
            )
            self._agents[agent_id] = rec
            return rec

    # ------------------------------------------------------------------ public API
    def create_agent(self, task_config: TaskConfig) -> str:
        """Create (but do not start) a hardened container for the task. Returns agent_id.

        Docker-side creation problems do not raise: the agent is registered as FAILED
        with a structured result so that callers always get a classified outcome.
        """
        task_config.validate()
        rec = self._new_record(task_config)
        cfg = task_config
        spec = ContainerSpec(
            name=rec.container_name,
            image=cfg.image,
            labels={
                LABEL_PROJECT: LABEL_PROJECT_VALUE,
                LABEL_RUN_ID: self.run_id,
                LABEL_AGENT_ID: rec.agent_id,
                LABEL_TASK_ID: cfg.task_id,
                LABEL_CREATED_AT: str(int(time.time())),
            },
            env={"TASK_JSON": cfg.task_json(), "AGENT_ID": rec.agent_id},
            cpus=cfg.cpus,
            memory_mb=cfg.memory_mb,
            pids_limit=cfg.pids_limit,
        )
        try:
            if not self.docker.image_exists(cfg.image):
                self._finish(
                    rec, AgentState.FAILED, ErrorKind.INFRASTRUCTURE,
                    f"image {cfg.image!r} not found; run `python -m orchestrator build`",
                )
                return rec.agent_id
            rec.container_id = self.docker.create_container(spec)
        except DockerError as exc:
            kind = ErrorKind.INFRASTRUCTURE if exc.unavailable else ErrorKind.STARTUP_FAILURE
            self._finish(rec, AgentState.FAILED, kind, f"container creation failed: {exc}")
        return rec.agent_id

    def start_agent(self, agent_id: str) -> None:
        """Start a CREATED agent. Blocks while the concurrency limit is reached."""
        rec = self._get(agent_id)
        if rec.state is not AgentState.CREATED:
            raise InvalidTransition(f"{agent_id} is {rec.state.value}, expected CREATED")
        if not self._acquire_slot(rec):
            self._finish(rec, AgentState.CANCELLED, ErrorKind.CANCELLED, "cancelled before start")
            return
        try:
            with rec.lock:
                rec.sm.transition(AgentState.STARTING)
        except InvalidTransition:  # cancelled while waiting for a slot
            self._release_slot(rec)
            return
        try:
            self.docker.start_container(rec.container_id)
        except DockerError as exc:
            kind = ErrorKind.INFRASTRUCTURE if exc.unavailable else ErrorKind.STARTUP_FAILURE
            self._finish(rec, AgentState.FAILED, kind, f"container start failed: {exc}")
            return
        with rec.lock:
            rec.started_monotonic = time.monotonic()
            rec.deadline = rec.started_monotonic + rec.config.timeout_s
            try:
                rec.sm.transition(AgentState.RUNNING)
            except InvalidTransition:  # cancelled while starting; make sure it is stopped
                self._safe(self.docker.kill_container, rec.container_id)

    def get_agent_status(self, agent_id: str) -> dict:
        rec = self._get(agent_id)
        status = {
            "agent_id": agent_id,
            "task_id": rec.config.task_id,
            "run_id": self.run_id,
            "state": rec.state.value,
            "container_id": rec.container_id,
            "container_name": rec.container_name,
            "docker_status": None,
            "exit_code": None,
            "history": [(s.value, t) for s, t in rec.sm.history],
        }
        if rec.container_id and rec.state is not AgentState.REMOVED:
            try:
                st = self.docker.inspect_container(rec.container_id)["State"]
                status["docker_status"], status["exit_code"] = st.get("Status"), st.get("ExitCode")
            except DockerError as exc:
                status["docker_status"] = "absent" if exc.not_found else "unknown"
        return status

    def collect_agent_result(self, agent_id: str) -> AgentResult:
        """Wait for the agent to finish (enforcing its timeout), classify and return the result."""
        rec = self._get(agent_id)
        if rec.result is not None:
            return rec.result
        if rec.state is not AgentState.RUNNING:
            raise InvalidTransition(f"{agent_id} is {rec.state.value}, expected RUNNING")
        info, infra_error = self._wait_for_exit(rec)
        with rec.lock:
            if rec.result is not None:
                return rec.result
            if info is None:
                return self._finish(rec, AgentState.FAILED, ErrorKind.INFRASTRUCTURE, infra_error)
            return self._classify_and_finish(rec, info)

    def stop_agent(self, agent_id: str, grace_s: int = 2) -> AgentState:
        """Cancel an agent: SIGTERM, then SIGKILL after grace_s. Idempotent."""
        rec = self._get(agent_id)
        self._cancel(rec, grace_s)
        return rec.state

    def destroy_agent(self, agent_id: str) -> None:
        """Remove the container (cancelling it first if needed). Idempotent and retryable."""
        rec = self._get(agent_id)
        if rec.state is AgentState.REMOVED:
            return
        with rec.lock:
            if rec.state not in TERMINAL_STATES and rec.state is not AgentState.CLEANUP:
                self._cancel(rec, grace_s=0)
            if rec.state in TERMINAL_STATES:
                rec.sm.transition(AgentState.CLEANUP)
            if rec.container_id:
                self.docker.remove_container(rec.container_id)  # tolerates "no such container"
            rec.sm.transition(AgentState.REMOVED)
            self._release_slot(rec)

    def run_agent(self, task_config: TaskConfig) -> AgentResult:
        """create -> start -> collect -> destroy, with cleanup guaranteed on every path."""
        agent_id = self.create_agent(task_config)
        try:
            rec = self._get(agent_id)
            if rec.state is AgentState.CREATED:
                self.start_agent(agent_id)
            return self.collect_agent_result(agent_id)
        except BaseException:
            self._safe(self.stop_agent, agent_id, 0)
            raise
        finally:
            self.destroy_agent(agent_id)

    def run_many(self, task_configs: Iterable[TaskConfig]) -> list:
        """Run tasks concurrently (at most max_concurrency at a time); results keep input order."""
        pool = ThreadPoolExecutor(max_workers=self.max_concurrency, thread_name_prefix="agent")
        futures = [pool.submit(self.run_agent, cfg) for cfg in task_configs]
        try:
            return [f.result() for f in futures]
        except BaseException:
            self.shutdown_agents()
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        finally:
            pool.shutdown(wait=True)

    # ------------------------------------------------------------------ cleanup / shutdown
    def shutdown_agents(self) -> None:
        """Refuse new agents, cancel running ones and remove every container of this run."""
        self._closing = True
        for rec in list(self._agents.values()):
            self._safe(self._cancel, rec, 0)
        for agent_id in list(self._agents):
            self._safe(self.destroy_agent, agent_id)

    def close(self) -> None:
        self.shutdown_agents()
        self._lease.release()

    def cleanup_stale(self) -> dict:
        """Remove containers of dead runs. Never touches this run, active runs or unlabeled containers.

        A container is removed only if it carries the project label AND a run-id label
        AND that run's lease is not held by any live process. Phase 2 task containers
        (label multiagent.poc.kind=agent-task) are never removed here; see TaskManager.reconcile().
        """
        removed, active, unowned, tasks = [], set(), [], []
        by_run: dict[str, list] = {}
        for c in self.docker.list_containers(_PROJECT_FILTER):
            labels = c["Config"].get("Labels") or {}
            run_id = labels.get(LABEL_RUN_ID)
            if labels.get(LABEL_KIND) == KIND_TASK:
                tasks.append(c["Id"])  # Phase 2 tasks: TaskManager.reconcile() adopts or finalises them
            elif run_id:
                by_run.setdefault(run_id, []).append(c)
            else:
                unowned.append(c["Id"])
        for run_id, containers in by_run.items():
            if run_id == self.run_id:
                continue
            if RunLease.is_active(self.state_dir, run_id):
                active.add(run_id)
                continue
            for c in containers:
                try:
                    self.docker.remove_container(c["Id"])
                    removed.append(c["Id"])
                except DockerError as exc:
                    log.warning("could not remove stale container %s: %s", c["Id"][:12], exc)
            RunLease.forget(self.state_dir, run_id)
        return {
            "removed_containers": removed,
            "skipped_active_runs": sorted(active),
            "skipped_unowned_containers": unowned,
            "skipped_task_containers": tasks,
        }

    def list_managed(self) -> list:
        """All containers carrying the project label, with ownership classification."""
        rows = []
        for c in self.docker.list_containers(_PROJECT_FILTER):
            labels = c["Config"].get("Labels") or {}
            run_id = labels.get(LABEL_RUN_ID)
            if not run_id:
                owner = "unowned"
            elif run_id == self.run_id:
                owner = "this-run"
            else:
                owner = "active" if RunLease.is_active(self.state_dir, run_id) else "stale"
            rows.append(
                {
                    "container_id": c["Id"][:12],
                    "name": c["Name"].lstrip("/"),
                    "run_id": run_id,
                    "agent_id": labels.get(LABEL_AGENT_ID),
                    "task_id": labels.get(LABEL_TASK_ID),
                    "docker_state": c["State"]["Status"],
                    "owner": owner,
                }
            )
        return rows

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _safe(fn, *args):
        try:
            fn(*args)
        except Exception as exc:
            log.warning("%s failed: %s", getattr(fn, "__name__", fn), exc)

    def _acquire_slot(self, rec: AgentRecord) -> bool:
        while not self._slots.acquire(timeout=0.2):
            if rec.cancel_requested.is_set() or self._closing:
                return False
        if rec.cancel_requested.is_set() or self._closing:
            self._slots.release()
            return False
        with rec.lock:
            rec.slot_held = True
        return True

    def _release_slot(self, rec: AgentRecord) -> None:
        with rec.lock:
            if rec.slot_held:
                rec.slot_held = False
                self._slots.release()

    def _cancel(self, rec: AgentRecord, grace_s: int) -> None:
        rec.cancel_requested.set()
        state = rec.state
        if state is AgentState.CREATED:
            self._finish(rec, AgentState.CANCELLED, ErrorKind.CANCELLED, "cancelled before start")
        elif state in (AgentState.STARTING, AgentState.RUNNING):
            try:
                self.docker.stop_container(rec.container_id, grace_s)
                self.docker.kill_container(rec.container_id)
            except DockerError as exc:
                log.warning("stopping %s failed: %s", rec.agent_id, exc)
            if rec.state is AgentState.RUNNING:
                self.collect_agent_result(rec.agent_id)
            elif rec.state is AgentState.STARTING:
                self._finish(rec, AgentState.CANCELLED, ErrorKind.CANCELLED, "cancelled while starting")

    def _wait_for_exit(self, rec: AgentRecord):
        """Poll until the container has exited, killing it at the deadline.

        Returns (inspect_info, None) or (None, error_message) on infrastructure failure.
        """
        failures, kill_sent_at = 0, None
        while True:
            try:
                info = self.docker.inspect_container(rec.container_id)
                failures = 0
            except DockerError as exc:
                if exc.not_found:
                    if rec.cancel_requested.is_set():
                        return None, "cancelled"
                    return None, "container disappeared while running"
                failures += 1
                if failures >= 3:
                    return None, f"lost contact with docker: {exc}"
                time.sleep(self.poll_interval_s)
                continue
            if info["State"].get("Status") not in _LIVE_DOCKER_STATES:
                return info, None
            now = time.monotonic()
            if kill_sent_at is None and now >= rec.deadline:
                rec.timed_out = True
                kill_sent_at = now
                self._safe(self.docker.kill_container, rec.container_id)
            elif kill_sent_at is not None and now - kill_sent_at > 15:
                return None, "container did not exit after kill"
            time.sleep(self.poll_interval_s)

    def _read_agent_result(self, rec: AgentRecord):
        """Return (agent_result_dict | None, stderr_tail). Output is treated as untrusted."""
        try:
            stdout, stderr = self.docker.logs(rec.container_id)
        except DockerError:
            return None, ""
        parsed = None
        for line in reversed(stdout.splitlines()):
            if line.startswith(RESULT_PREFIX):
                payload = line[len(RESULT_PREFIX):]
                if len(payload) > MAX_RESULT_BYTES:
                    break
                try:
                    candidate = json.loads(payload)
                except ValueError:
                    break
                if (
                    isinstance(candidate, dict)
                    and candidate.get("task_id") == rec.config.task_id
                    and candidate.get("status") in ("completed", "failed")
                    and isinstance(candidate.get("output"), (dict, type(None)))
                ):
                    parsed = candidate
                break
        return parsed, stderr[-500:].strip()

    def _classify_and_finish(self, rec: AgentRecord, info: dict) -> AgentResult:
        st = info["State"]
        code = st.get("ExitCode")
        started, finished = _parse_docker_time(st.get("StartedAt")), _parse_docker_time(st.get("FinishedAt"))
        times = {"started_at": started, "finished_at": finished}
        if rec.timed_out:
            return self._finish(
                rec, AgentState.TIMED_OUT, ErrorKind.TIMEOUT,
                f"exceeded timeout of {rec.config.timeout_s}s; container killed",
                exit_code=code, **times,
            )
        agent, stderr_tail = self._read_agent_result(rec)
        completed_ok = code == 0 and agent is not None and agent["status"] == "completed"
        if rec.cancel_requested.is_set() and not completed_ok:
            return self._finish(
                rec, AgentState.CANCELLED, ErrorKind.CANCELLED, "cancelled by orchestrator",
                exit_code=code, **times,
            )
        if completed_ok:
            return self._finish(rec, AgentState.COMPLETED, None, None, exit_code=0, output=agent["output"], **times)
        if code == 0:
            return self._finish(
                rec, AgentState.FAILED, ErrorKind.APP_FAILURE,
                "container exited 0 but returned no valid completed result",
                exit_code=code, **times,
            )
        if st.get("Error") or (code in (126, 127) and agent is None):
            return self._finish(
                rec, AgentState.FAILED, ErrorKind.STARTUP_FAILURE,
                f"container could not run its command: {st.get('Error') or stderr_tail}",
                exit_code=code, **times,
            )
        if st.get("OOMKilled"):
            message, detail = "agent killed: out of memory (OOMKilled)", None
        elif agent is not None and agent["status"] == "failed":
            err = agent.get("error")
            message = err.get("message") if isinstance(err, dict) else str(err or "agent reported failure")
            detail = err.get("type") if isinstance(err, dict) else None
        else:
            message, detail = f"agent exited with code {code}", None
            if stderr_tail:
                message += f": {stderr_tail}"
        return self._finish(
            rec, AgentState.FAILED, ErrorKind.APP_FAILURE, message,
            exit_code=code, output=agent.get("output") if agent else None,
            error_type=detail, **times,
        )

    def _finish(
        self,
        rec: AgentRecord,
        state: AgentState,
        kind: Optional[ErrorKind],
        message: Optional[str],
        *,
        exit_code: Optional[int] = None,
        output: Optional[dict] = None,
        error_type: Optional[str] = None,
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None,
    ) -> AgentResult:
        """Move to a terminal state exactly once, record + persist the result, free the slot."""
        with rec.lock:
            if rec.result is not None:
                return rec.result
            rec.sm.transition(state)
            if started_at and finished_at and finished_at >= started_at:
                duration = int((finished_at - started_at).total_seconds() * 1000)
            elif rec.started_monotonic is not None:
                duration = int((time.monotonic() - rec.started_monotonic) * 1000)
            else:
                duration = None
            error = None
            if kind is not None:
                error = {"kind": kind.value, "message": message}
                if error_type:
                    error["type"] = error_type
            rec.result = AgentResult(
                task_id=rec.config.task_id,
                agent_id=rec.agent_id,
                status=state.value.lower(),
                exit_code=exit_code,
                duration_ms=duration,
                output=output,
                error=error,
                container_id=rec.container_id,
                started_at=_iso(started_at),
                finished_at=_iso(finished_at),
            )
            self._persist(rec)
            self._release_slot(rec)
            return rec.result

    def _persist(self, rec: AgentRecord) -> None:
        """Store the structured result under workspaces/<agent_id>/result.json (host-side, never mounted)."""
        try:
            directory = self.workspace_root / rec.agent_id
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            (directory / "result.json").write_text(json.dumps(rec.result.to_dict(), indent=2))
        except OSError as exc:
            log.warning("could not persist result for %s: %s", rec.agent_id, exc)
