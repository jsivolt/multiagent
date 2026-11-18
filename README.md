# Dynamic Agent Container POC (Phase 1)

An **Orchestrator** (Python 3, standard library only) that dynamically creates, runs, monitors and
destroys isolated Docker containers, one per task. Each container runs a **mock Agent**
(`echo`, `sleep`, `compute`, `fail`; no LLM, no network). Phase 2 swaps the mock for a real coding agent.

## Architecture

```text
 CLI (python -m orchestrator)
          |
    Orchestrator  ---- run lease (flock) ---- workspaces/.runs/<run_id>.lock
    manager.py            |
          | docker CLI -> Docker Engine
   +------+-------+--------+
   v              v        v
 Agent A       Agent B   Agent C        hardened containers, tmpfs /workspace
   |              |        |
   +--- result line on stdout --> collected --> workspaces/<agent_id>/result.json --> container removed
```

| File | Role |
|---|---|
| `orchestrator/manager.py` | `Orchestrator`: `create_agent`, `start_agent`, `get_agent_status`, `collect_agent_result`, `stop_agent`, `destroy_agent`, `run_agent`, `run_many`, `cleanup_stale` |
| `orchestrator/lifecycle.py` | State machine (`CREATED → STARTING → RUNNING → COMPLETED/FAILED/TIMED_OUT/CANCELLED → CLEANUP → REMOVED`) and the crash-safe run lease |
| `orchestrator/models.py` | `TaskConfig`, `AgentResult`, labels, defaults |
| `orchestrator/docker_client.py` | The only place docker commands are built; all hardening flags live in `build_create_args` |
| `agent/` | Mock agent image (`python:3.12-alpine`, non-root uid 10001) |

Key behaviours:

* **Unique IDs**: `agent-<8 hex>`, container `poc-<run_id>-<agent_id>`; every container is labelled
  `multiagent.poc=phase1`, `multiagent.poc.run_id`, `.agent_id`, `.task_id`, `.created_at`.
* **Concurrency**: `--max-concurrency` (default 4) bounds containers started/running at once (slot taken in
  `start_agent`, released at the terminal state). `run_many` uses a thread pool.
* **Timeout**: measured from container start; on expiry the container is `docker kill`ed and the task reports `timed_out`.
* **Result protocol**: the agent writes `result.json` in its tmpfs workspace and prints the same JSON as one
  `@@AGENT_RESULT@@{...}` stdout line. The workspace vanishes when the container stops, so the orchestrator reads the
  stdout line (before removing the container) and stores the final structured result at
  `workspaces/<agent_id>/result.json` (host side, never mounted into any container). Only that file is preserved.
* **Outcome classification** (`error.kind`): `app_failure`, `startup_failure`, `timeout`, `cancelled`,
  `infrastructure`. Success requires exit code 0 **and** a valid `completed` result for the right `task_id`;
  a container that merely started/exited 0 is reported as failed.
* **Guaranteed cleanup**: `run_agent` destroys the container in `finally`; `destroy_agent` is idempotent and
  retryable; the CLI converts SIGINT/SIGTERM into cleanup of every container of the run.
* **Orphan recovery**: each orchestrator holds an exclusive `flock` on `workspaces/.runs/<run_id>.lock`. The kernel
  drops the lock when the process dies (even `kill -9`). `cleanup --stale` removes a container only if it has the project
  label **and** a `run_id` label **and** that run's lock is free. Containers of live runs, of the current run, with only
  the project label, or unrelated are never touched.

Result example:

```json
{"task_id": "task-001", "agent_id": "agent-1a2b3c4d", "status": "completed", "exit_code": 0,
 "duration_ms": 252, "output": {"message": "Hello Agent"}, "error": null,
 "container_id": "…", "started_at": "…", "finished_at": "…"}
```

## Prerequisites

* Linux, Python 3 (tested on 3.12), `docker` CLI + running daemon, user able to run `docker` (group `docker`).
* Base image `python:3.12-alpine` (pulled on first build). cgroup v2 is assumed by the limit-probing tests.
* No third-party packages for the orchestrator. `pytest` only for tests.

## Build

```bash
python3 -m orchestrator build              # builds multiagent-poc-agent:phase1 from agent/
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # test dependency only
```

## CLI usage

```bash
python -m orchestrator run --type echo --message "Hello Agent"
python -m orchestrator run --type sleep --duration 60 --timeout 5      # -> status timed_out, exit code 1
python -m orchestrator run --type compute --n 100000
python -m orchestrator run --type fail --exit-code 3 --message boom
python -m orchestrator demo --agents 3 [--with-failure] [--duration 2] [--max-concurrency N]
python -m orchestrator status                                          # managed containers + owner (this-run/active/stale/unowned)
python -m orchestrator cleanup --stale                                 # remove containers of dead runs only
```

Common options: `--workspace-root` (default `./workspaces` or `$MULTIAGENT_WORKSPACES`), `--image`,
`--max-concurrency`; limits: `--timeout 30`, `--cpus 0.5`, `--memory-mb 256`, `--pids 64`.
`run` exits 0 only for `completed`. `run`/`demo` also clean up stale runs at start-up.

Library use:

```python
from orchestrator import Orchestrator, TaskConfig
with Orchestrator(max_concurrency=3) as orch:
    result = orch.run_agent(TaskConfig(type="echo", params={"message": "hi"}))
```

## Security model

Per container (fixed in `build_create_args`, not overridable per task): `--network none`, `--read-only` rootfs,
only `/workspace` writable (16 MB tmpfs, `noexec,nosuid,nodev`, owned by uid 10001), user `10001:10001`,
`--cap-drop ALL`, `no-new-privileges`, Docker's default seccomp profile (never `unconfined`), `--cpus 0.5`,
`--memory 256m` with swap disabled, `--pids-limit 64`, no volumes/bind mounts at all (so no docker socket, no home
directory), not privileged, no host network/PID/IPC, no environment passed except the task JSON and agent ID, log
size capped at 1 MB. The agent's output is treated as untrusted: size-capped, `task_id` checked, and status derived
from the real exit code rather than from what the agent claims.

**Limitations**: Docker is a basic isolation boundary, not a hardened one; containers share the host kernel and
a kernel/runtime escape defeats everything above. Anyone able to run the orchestrator has `docker` access, i.e. is
effectively root on the host. Not rootless, no user-namespace remapping, no gVisor/Kata (see Phase 2).

## Tests

```bash
.venv/bin/python -m pytest -q                          # everything (needs Docker)
.venv/bin/python -m pytest -q -m "not live"            # unit tests only, no Docker needed
.venv/bin/python -m pytest -q tests/test_isolation.py  # live isolation / resource-limit checks
```

* `test_lifecycle.py`, `test_orchestrator.py`: state machine, run lease (incl. SIGKILLed owner), classification,
  concurrency limit, cleanup selection. `test_orchestrator.py` uses an **in-memory fake Docker**: it proves logic only.
* `test_isolation.py` (live): inspects the real container configuration **and** probes enforcement from inside
  (uid, capabilities, seccomp, read-only rootfs, noexec workspace, no network, no socket/home, fork limit, OOM kill,
  CPU throttling, cross-agent workspace separation).
* `test_integration.py` (live): creation, success, parallelism, concurrency cap observed via `docker ps`, failure
  isolation, timeout kill (host PID gone), orphan recovery with real SIGKILLed orchestrators and decoy containers,
  20 sequential tasks without leaks, interrupt cleanup, CLI.

Live tests skip if the daemon is unreachable and remove everything they create.

## Known limitations

* In-memory registry only: a restarted orchestrator cannot re-attach to running agents (it removes them as stale).
* Result transport is the container log: a task that floods stdout past the 1 MB log cap loses its result line and
  is reported as failed (safe direction). Agent artifacts other than the result are not preserved.
* Run leases use `fcntl.flock` (Linux/macOS); containers are removed, not "adopted".
* Timeout precision is the poll interval (0.1 s) plus docker kill latency. CPU/memory limits assume cgroup v2.
* Shared-host caution: the cleanup logic only matches this POC's labels, but concurrent test runs on a busy host
  can still affect `docker ps`-based timing assertions.

## Phase 2 considerations

* Replace `agent/agent.py::run_task` with the real agent; keep the stdout/exit-code result contract or move to a
  streaming channel (stdout log-driver limits) / dedicated results volume with a size quota.
* Real agents need network and credentials: use an egress proxy/allow-list network, per-task short-lived scoped
  secrets injected by the orchestrator (never the host environment), and per-task budgets.
* Stronger isolation: rootless Docker or userns-remap, gVisor/Kata runtime, custom seccomp profile, disk quotas.
* Persist the registry (SQLite/Redis) for re-attachment and audit, add a work queue with priorities/backpressure,
  structured logging/metrics, and an async Docker API client if scale demands it.
* Workspace for code tasks: per-task volume/clone with size quota, collected via `docker cp`/archive into an artifact store.
