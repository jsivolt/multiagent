# Phase 1 Report — Dynamic Agent Container POC

Date: 2026-11-18. Status: **Phase 1 complete**; all 72 tests pass (43 unit, 29 live Docker).

## 1. Host findings (before any change)

| Item | Finding |
|---|---|
| OS / arch | Ubuntu 22.04, Linux 6.8, x86_64 |
| Docker | Engine 28.5.0 (API 1.51), runc, overlay2, cgroup v2 (systemd driver) |
| Docker security | default seccomp profile + AppArmor + cgroupns; **not rootless**; user namespaces not enabled |
| Permissions | user `celltester` is in the `docker` group |
| Resources | 12 CPUs, ~62 GB RAM, ~117 GB free disk |
| Existing Docker state | ~8 unrelated running containers (Nextcloud, authentik, open-webui, redis, postgres) and 2 exited ones; none touched |
| Repository | empty git repo, no commits, no existing agent code |
| Tooling | Python 3.12; no `docker` SDK, no pytest, broken system `pip` (worked around with a venv) |

No system packages were installed. Only `python:3.12-alpine` was pulled and a project-local `.venv` created.

## 2. Design decisions

* **Stdlib-only orchestrator** driving the `docker` CLI (no SDK dependency, simple to audit).
* **Workspace**: each container gets a 16 MB `noexec,nosuid,nodev` tmpfs at `/workspace`. A tmpfs volume is
  unmounted when the container stops, so its files cannot be copied out afterwards. The agent therefore also prints its
  result as a `@@AGENT_RESULT@@{json}` stdout line; the orchestrator reads it from `docker logs` before removing the
  container and stores the result in `workspaces/<agent_id>/result.json` on the host (never mounted into a container).
* **Ownership / orphan recovery**: labels (`multiagent.poc=phase1`, `multiagent.poc.run_id`, ...) plus an exclusive
  `flock` on `workspaces/.runs/<run_id>.lock`. The kernel releases the lock when the process dies, so a free lock
  means a stale run. Cleanup requires project label AND run-id label AND free lock.
* **Success rule**: exit code 0 AND a valid `completed` result with matching `task_id`. Starting and exiting 0 is not success.

## 3. Files created

| File | Purpose |
|---|---|
| `orchestrator/manager.py` | `Orchestrator` (lifecycle API, concurrency, timeout, classification, cleanup) |
| `orchestrator/lifecycle.py` | state machine and run lease |
| `orchestrator/models.py` | `TaskConfig`, `AgentResult`, labels, validation |
| `orchestrator/docker_client.py` | docker CLI wrapper; all hardening flags in `build_create_args` |
| `orchestrator/__main__.py`, `__init__.py` | CLI and package exports |
| `agent/agent.py`, `agent/Dockerfile` | mock agent (`echo`, `sleep`, `compute`, `fail`), non-root image |
| `tests/test_lifecycle.py`, `test_orchestrator.py` | unit tests (`test_orchestrator.py` uses `tests/fake_docker.py`) |
| `tests/test_isolation.py`, `test_integration.py` | live Docker tests (`tests/hold_run.py` is a helper process) |
| `tests/conftest.py`, `pytest.ini`, `requirements.txt`, `.gitignore`, `README.md`, `REPORT.md` | support and docs |

Deviation from the suggested layout: files live at the repo root rather than under `multi-agent-poc/`, and
`docker_client.py`, `fake_docker.py`, `hold_run.py`, `conftest.py` were added.

## 4. Commands executed (summary)

```bash
python3 -m orchestrator build
python3 -m orchestrator run --type echo --message "Hello Agent"             # completed, 252 ms
python3 -m orchestrator run --type sleep --duration 60 --timeout 3          # timed_out, exit 137, ~3.1 s
python3 -m orchestrator demo --agents 3 --with-failure --duration 2         # 2 completed + 1 failed, overlap 3
python3 -m orchestrator status                                              # "no managed containers"
.venv/bin/python -m pytest -v                                               # 72 passed in 49 s
```

## 5. Test results

| Group | Tests | Result |
|---|---|---|
| `test_lifecycle.py` (state machine, lease incl. SIGKILLed owner) | 15 | PASS |
| `test_orchestrator.py` (fake Docker: logic only) | 28 | PASS |
| `test_isolation.py` (live: Tests 6 and 7) | 13 | PASS |
| `test_integration.py` (live: Tests 1-5, 8, 9, cancel, interrupt cleanup, CLI) | 16 | PASS |
| **Total** | **72** | **72 PASS, 0 FAIL, 0 SKIPPED** |

Spec test mapping:

| Spec test | Evidence |
|---|---|
| 1 Dynamic creation | unique container IDs, image, `NanoCpus`/`Memory`/`PidsLimit`, labels checked via `docker inspect` |
| 2 Successful execution | echo returns structured result, exit 0, container gone after `destroy_agent` |
| 3 Parallel execution | 3 x 2 s sleeps finish in < 5.5 s, overlap = 3; cap of 2 observed via `docker ps` (peak exactly 2) |
| 4 Failure isolation | failing task reports exit 7 / `app_failure`; sibling tasks complete; no leftovers |
| 5 Timeout | `timed_out`, exit 137, host PID absent from `/proc`, container removed |
| 6 Resource limits | config plus enforcement from inside: `pids.max=64`, `memory.max=256MiB`, swap 0, `cpu.max=50000 100000`; fork loop blocked; 600 MB allocation OOM-killed; busy loop throttled below 0.65 CPU |
| 7 Security isolation | see section 6 |
| 8 Orphan recovery | real SIGKILLed orchestrator + decoys: only stale owned containers removed; active run, unlabeled, project-label-only and pre-existing containers preserved |
| 9 Repeated execution | 20 sequential tasks: distinct containers, consistent results, no leaked containers or volumes |

Test validity: hardening flags were temporarily weakened (network, read-only, cap-drop, seccomp, PID limit, CPU,
user); 9 of 13 isolation tests failed, confirming the tests detect weakened isolation. The file was then restored.
The fake-Docker tests are logic tests only and are not counted as isolation proof.

## 6. Docker container evidence

Live agent container (`docker ps` / `docker inspect`):

```text
6c0757bc2663 poc-8077d9474648-agent-1ee40767 multiagent-poc-agent:phase1 Up
NanoCpus=500000000 Memory=268435456 MemorySwap=268435456 PidsLimit=64
NetworkMode=none ReadonlyRootfs=True CapDrop=[ALL] SecurityOpt=[no-new-privileges=true] Privileged=False
Tmpfs={/workspace: rw,noexec,nosuid,nodev,size=16m,uid=10001,gid=10001,mode=0700}  User=10001:10001  Mounts=[]
Labels: multiagent.poc=phase1, .run_id=8077d9474648, .agent_id=agent-1ee40767, .task_id=task-e8f1427f
```

Security verification (live, from inside running containers): uid 10001; `CapBnd`/`CapEff` = 0; `NoNewPrivs=1`;
`Seccomp=2`; writes outside `/workspace` fail; binaries in `/workspace` cannot execute; only `lo` network interface and
outbound connect fails; `/var/run/docker.sock`, the host home and the host workspace path are absent; `su`/`setuid(0)`
fail; a second agent sees an empty `/workspace` and no shared mounts.

After the full suite: 0 containers labeled `multiagent.poc`, 0 decoys, and the host's container ID set was identical
before and after.

## 7. Known issues and limitations

* Docker is a basic isolation boundary: containers share the host kernel; users who can run the orchestrator effectively
  have root on the host. Not rootless; no userns remap, gVisor or Kata.
* In-memory registry: a restarted orchestrator cannot re-attach to running agents; it removes them as stale.
* Result transport is the container log (1 MB cap). Overflow loses the result line and the task is reported failed.
* Timeout precision is the 0.1 s poll interval plus docker kill latency.
* Limit-probing tests assume cgroup v2; run leases need POSIX `flock`.
* `docker ps`-based timing assertions could be disturbed by heavy unrelated load on a shared host.

## 8. Recommended Phase 2 changes

1. Replace `agent/agent.py::run_task` with the real coding agent; keep the exit-code + validated-result contract or move to a dedicated results channel with size quota.
2. Controlled egress (allow-list proxy network) and per-task short-lived scoped secrets injected by the orchestrator, never from host env.
3. Stronger isolation: rootless Docker or userns-remap, gVisor/Kata runtime, custom seccomp profile, disk quotas.
4. Persistent registry (SQLite/Redis) for re-attachment and audit; work queue with backpressure; structured logs and metrics.
5. Per-task code workspaces with quota, collected via archive/`docker cp` into an artifact store.
