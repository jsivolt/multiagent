"""In-memory stand-in for DockerClient, used ONLY for unit tests of orchestrator logic.

It proves nothing about real isolation; live behaviour is covered by test_isolation.py
and test_integration.py, which talk to the real Docker daemon.
"""
import json
import threading
import time
import uuid
from datetime import datetime, timezone

from orchestrator.docker_client import DockerError
from orchestrator.models import RESULT_PREFIX


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class FakeDocker:
    def __init__(self):
        self.containers = {}
        self.images = {"multiagent-poc-agent:phase1"}
        self.lock = threading.Lock()
        self.peak_running = 0
        self.create_error = None
        self.start_error = None
        self.behavior = {}  # task type -> dict(exit_code=, result=, oom=, error=, hang=)

    # -- helpers ------------------------------------------------------------------
    def _task(self, c):
        return json.loads(c["spec"].env["TASK_JSON"])

    def _state(self, c):
        """Lazily compute container state from the clock."""
        if c["exited"] is None and c["started"] is not None:
            now = time.monotonic()
            if now >= c["finish_at"]:
                self._exit(c, c["plan"]["exit_code"])
        return c

    def _exit(self, c, code):
        if c["exited"] is None:
            c["exited"] = code
            c["finished_iso"] = _now()

    def _running(self):
        return [c for c in self.containers.values() if c["started"] and self._state(c)["exited"] is None]

    def _plan(self, task):
        kind, params = task["type"], task.get("params", {})
        plan = {"exit_code": 0, "result": {"status": "completed", "output": {}, "error": None},
                "oom": False, "error": "", "duration": 0.0}
        if kind == "sleep":
            plan["duration"] = float(params.get("duration", 0))
        if kind == "fail":
            plan["exit_code"] = 1
            plan["result"] = {"status": "failed", "output": None,
                              "error": {"type": "task_failed", "message": "intentional failure"}}
        plan.update(self.behavior.get(kind, {}))
        return plan

    # -- DockerClient interface -----------------------------------------------------
    def image_exists(self, image):
        return image in self.images

    def create_container(self, spec):
        if self.create_error:
            raise self.create_error
        cid = uuid.uuid4().hex + uuid.uuid4().hex
        with self.lock:
            self.containers[cid] = {"id": cid, "spec": spec, "started": None, "exited": None,
                                    "plan": self._plan(json.loads(spec.env["TASK_JSON"]))}
        return cid

    def start_container(self, cid):
        if self.start_error:
            raise self.start_error
        with self.lock:
            c = self.containers[cid]
            c["started"], c["started_iso"] = time.monotonic(), _now()
            c["finish_at"] = c["started"] + (10_000 if c["plan"].get("hang") else c["plan"]["duration"])
            self.peak_running = max(self.peak_running, len(self._running()))

    def inspect_container(self, cid):
        with self.lock:
            c = self.containers.get(cid)
            if c is None:
                raise DockerError("No such container", not_found=True)
            self._state(c)
            running = c["started"] is not None and c["exited"] is None
            return {
                "Id": c["id"],
                "Name": "/" + c["spec"].name,
                "Config": {"Labels": dict(c["spec"].labels)},
                "State": {
                    "Status": "running" if running else ("created" if c["started"] is None else "exited"),
                    "Running": running,
                    "ExitCode": c["exited"] or 0,
                    "OOMKilled": c["plan"]["oom"] and c["exited"] is not None,
                    "Error": c["plan"]["error"] if c["exited"] is not None else "",
                    "StartedAt": c.get("started_iso", "0001-01-01T00:00:00Z"),
                    "FinishedAt": c.get("finished_iso", "0001-01-01T00:00:00Z"),
                },
            }

    def _terminate(self, cid):
        with self.lock:
            c = self.containers.get(cid)
            if c and c["started"] and self._state(c)["exited"] is None:
                self._exit(c, 137)

    stop_container = lambda self, cid, grace_s=2: self._terminate(cid)  # noqa: E731
    kill_container = lambda self, cid: self._terminate(cid)  # noqa: E731

    def remove_container(self, cid):
        with self.lock:
            self.containers.pop(cid, None)

    def logs(self, cid, tail=200):
        with self.lock:
            c = self.containers.get(cid)
            if c is None:
                raise DockerError("No such container", not_found=True)
            self._state(c)
            if c["exited"] is None or c["exited"] == 137 or c["plan"]["result"] is None:
                return "", ""
            task = self._task(c)
            result = {"task_id": task["task_id"], "agent_id": c["spec"].env["AGENT_ID"],
                      "exit_code": c["exited"], **c["plan"]["result"]}
            return RESULT_PREFIX + json.dumps(result) + "\n", c["plan"].get("stderr", "")

    def list_containers(self, labels):
        with self.lock:
            ids = [cid for cid, c in self.containers.items()
                   if all(c["spec"].labels.get(k) == v for k, v in labels.items())]
        return [self.inspect_container(cid) for cid in ids]

    def add_foreign(self, labels, name="foreign"):
        """Register a container that this orchestrator did not create (e.g. a stale run's)."""
        from orchestrator.models import ContainerSpec

        spec = ContainerSpec(name=name, image="x", labels=labels,
                             env={"TASK_JSON": '{"task_id":"t","type":"echo","params":{}}', "AGENT_ID": "a"},
                             cpus=0.5, memory_mb=256, pids_limit=64)
        cid = self.create_container(spec)
        self.containers[cid]["plan"]["hang"] = True
        self.start_container(cid)
        return cid
