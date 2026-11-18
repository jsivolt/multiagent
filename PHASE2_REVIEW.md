# Phase 2 Independent Review Report

Reviewer: independent Review Agent. Date: 2026-11-18. No implementation file was modified; review scripts live outside the repo.

## Executive Summary
- **Review outcome:** CHANGES REQUIRED
- **Overall assessment:** Phase 2 genuinely integrates a real coding-agent CLI (Pi 1.5.2) in a well-hardened container, and
  the core path works: I submitted a new task (Fibonacci module + tests), and the agent wrote both files, ran pytest, fixed a
  failure it found and re-ran the tests to green. I then re-ran those tests myself in a separate read-only sandbox: 6/6 passed,
  and edge cases behave correctly. All 145 existing tests pass when run in isolation. Container hardening, workspace
  confinement, path-traversal rejection and redaction are solid.
  Restart reconciliation has one confirmed destructive defect, and the lifecycle and CLI have three reliability defects.
- **Critical blockers:** **P1-001.** `TaskManager.reconcile()` decides whether a run is still alive by looking only at the
  lease directory of its own workspace root. Run from any other root, including the CLI default `./workspaces` resolved
  against a different CWD, it removes the **live** task containers of other running orchestrators. I reproduced this
  deterministically. It also happened accidentally during this review: it killed three containers of a concurrently
  running e2e suite.

## Review Scope
- **Baseline:** `bbfaa55` "Add Phase 1 dynamic agent container POC".
- **Reviewed commit/worktree:** `50847b2` "Add Phase 2: real Pi coding agent runtime integration" (clean worktree).
- **Files reviewed:** every file in `git diff bbfaa55 50847b2` (25 files, +4852/-17) except `package-lock.json`, which was
  checked only by inspecting the pinned version. In full: `orchestrator/{tasks,workspace,llm_relay,docker_client,models,lifecycle,manager,__main__}.py`,
  `orchestrator/runtimes/{base,pi}.py`, `agent/pi/{Dockerfile,entrypoint.sh,llm-relay.js}`, all Phase 2 tests and helpers,
  `README.md`, `REPORT_PHASE2.md`.
- **Runtime reviewed:** Pi coding agent 1.5.2 (`@earendil-works/pi-coding-agent`, `pi --mode json`) in image
  `multiagent-poc-pi:phase2`. Model `qwen3-vl:8b` through local Ollama, reached through the host-side relay. Docker 28.5.0,
  Python 3.12.11, a shared host with 10 unrelated containers. The unrelated containers were snapshotted before testing and the
  set was verified unchanged afterwards.
- **Coding Agent's claims checked:** 145/145 tests pass, the real E2E tests A-F pass, and the Definition-of-Done table in
  `REPORT_PHASE2.md` section 6. Known limitations in section 8 were taken into account and are not re-reported as defects.

## Requirements Compliance
| Requirement | Status | Evidence |
|---|---|---|
| Real coding-agent CLI in an isolated container | **PASS** | Independent run: `docker top` shows `docker-init -- /opt/runtime/entrypoint.sh --mode json --provider ollama --model qwen3-vl:8b ...`, then `pi` and `node llm-relay.js`. `pi --version` inside the container prints 1.5.2. Relay counted 3 real model requests and 35,382 tokens. |
| Launched through a runtime adapter | PASS | `PiRuntime.prepare` / `PiParser` (`runtimes/pi.py`). The core in `tasks.py` has no Pi-specific logic. The probe runtime and the test `PiCliRuntime` plug in without core changes. |
| Natural-language task produces real source files | PASS | `fibonacci.py` and `test_fibonacci.py` created. The tool-call trace shows write → bash pytest (1 failed) → edit → pytest (6 passed). |
| Workspace persists after container removal | PASS | Files present on the host after `container.cleaned`; `docker ps -a` finds 0 containers for the task. |
| Status, logs, events, result available | PASS | `result.json`, `events.jsonl` and `stdout.log` are readable after REMOVED. `status=completed`, `exitCode=0`, artifacts carry sha256 hashes. |
| Cancellation and timeout | PASS | Probe and real-agent tests pass (TIMED_OUT, CANCELLED, SIGKILL fallback, cross-process cancel). |
| Agent vs infrastructure failures distinguishable | PASS | E2E D tests pass: model_error, auth_error, agent_error, llm_backend_unreachable, startup_failure, image_missing/uid_mismatch. |
| Workspace isolation, no traversal | PASS | Only three explicit mounts. IDs like `../../etc`, `a/../../x`, `.tasks`, `-rf` and workspace `../evil` are all rejected. Symlink/FIFO tests pass. |
| No privileged config, no docker socket, non-root | PASS | Effective `docker inspect`: `User=1000:1000 Privileged=false Network=none ReadonlyRootfs=true CapDrop=[ALL] no-new-privileges`, 2 CPUs / 2 GiB with no swap / 512 PIDs. Inside the container: `CapEff 0`, `NoNewPrivs 1`, `Seccomp 2`, no `/var/run/docker.sock`, only `lo`. |
| Restart reconciliation | **FAIL** | Same-root recovery works (test F passes), but reconciliation destroys other orchestrators' live tasks (P1-001), and the CLI without `--wait` cancels every task it adopts (P2-001). |
| Cleanup failure recorded and recoverable | **PARTIAL** | Recoverable by a later reconcile, but the failure is not recorded and the CLI hangs forever (P2-002). |
| Phase 1 tests still pass | PASS | All 72 Phase 1 tests pass (see Test Results). |

## Architecture Findings
- Runtime separation is clean and proportionate. `AgentRuntime.prepare` returns a declarative `LaunchPlan`, and
  `OutputParser.feed/classify/summary` normalises output. `start`, `getStatus` and `cancel` are generic and live in
  `TaskManager`, which is correct because they are identical for every containerised CLI. A second runtime needs no core
  change, as `tests/probe.py` and `PiCliRuntime` show.
- Lifecycle ownership is clear. `TaskManager` owns the state machine, with `PREPARING` added additively in `lifecycle.py`.
  Adapters never touch Docker. There are no circular imports. All mutable state is per-instance, except the runtime registry,
  which is append-only.
- Buffers are bounded: lines are capped at 256 KiB, log files at 20 MB, events at 20,000, diffs at 512 KiB and scans at 20k files.
- **Phase 3 concern (P2-004):** the concurrency cap is a per-process semaphore, and cross-process ownership relies on
  per-workspace-root lease directories. Two orchestrators therefore neither share a capacity limit nor reliably see each
  other's liveness. P1-001 is the concrete consequence of the second point.

## Security Findings
Enforced boundaries, verified on a live task container rather than taken from configuration:

- non-root user (host uid)
- all capabilities dropped
- no-new-privileges
- default seccomp
- read-only rootfs
- `--network none`
- CPU, memory, swap and PID limits
- private IPC, no host PID namespace
- no Docker socket, no host home
- writable mounts: only `/workspace` (this task's directory) plus tmpfs at `/tmp` and `/home/agent`, both noexec
- the prompt and `models.json` mounted read-only; secrets as 0400 files mounted read-only and deleted after the run
- the relay forwards only the allow-listed routes and model

Assumed, not enforced (already declared in `REPORT_PHASE2.md` section 8): a shared kernel with no userns remap or gVisor;
Docker-group membership is equivalent to host root; the disk quota is soft; the workspace is untrusted output
(`.git/hooks`, symlinks); and any injected credential is readable by agent code. I found no container-escape or host-access
path.

Residual issues:
- **P2-003:** an untrusted container can exhaust host-orchestrator threads through the relay socket.
- **P3-005:** the relay socket in `/tmp` is world-connectable on the host. This is equivalent to the Ollama access local
  users already have.

## Runtime Integration Findings
- It is a real integration, not a substitute. The pinned Pi package is installed in the image (`npm ci --ignore-scripts`,
  `pi --version` checked at build time). Execution is Pi's own JSONL stream. The relay counts real upstream model requests.
  Files are produced by Pi's `write`/`edit` tools, and tests are executed by Pi's `bash` tool inside the container. Nothing
  is hardcoded or simulated, and no work runs on the host.
- Success classification is stricter than "exit 0": it requires `agent_end` and no error or aborted stop reason
  (`PiParser.classify`, `runtimes/pi.py:113-124`). This satisfies the requirement that a failed task must not count as
  completed just because the container exited with status 0.
- Note (not a defect): `completed` means the agent finished cleanly and *reported* success. There is no objective
  acceptance check, such as re-running tests or requiring artifacts. In my run the claim was true (verified independently).
  Phase 3 orchestration across several agents should add an optional verification step.
- **P3-003:** runtime options are not validated as `ValidationError`; `contextWindow: "big"` raises a bare `ValueError`.

## Lifecycle and Reliability Findings
- Correct:
  - transitions
  - invalid-transition prevention
  - idempotent cancel
  - duplicate submission (an identical request is a no-op; a different one raises `TaskConflict`)
  - busy-workspace rejection
  - timeout and cancel racing natural completion (natural exit wins if observed first; a stop followed by a failing exit maps to TIMED_OUT/CANCELLED)
  - the result is persisted before container removal
  - log replay on adoption without duplicate events
- Defects:
  - P1-001: reconcile deletes other roots' live tasks.
  - P2-001: CLI `task reconcile` without `--wait` cancels the tasks it adopts.
  - P2-002: an unrecorded cleanup failure hangs the CLI.
  - P3-001: relay temp-dir leak.
  - P3-002: quota stop followed by a clean exit is reported as COMPLETED.

## Test Results
| Test | Status | Evidence |
|---|---|---|
| `pytest -m "not live"` (Phase 1+2 unit, 89) | PASS | `89 passed, 56 deselected in 9.68s` |
| `pytest -m "live and not e2e"` (Phase 1 live + Phase 2 probe, 45) | PASS | `45 passed in 107.44s`; includes all 29 Phase 1 live tests |
| `pytest -m e2e` (real Pi + Ollama, 11), run 1 | FAIL (invalid run) | 3 failed (B, C, D-infra) because my concurrent reconcile probes removed their live containers (`container_lost`). This run is the accidental reproduction of P1-001, not an implementation regression in those tests. |
| `pytest -m e2e`, run 2 (isolated) | PASS | `11 passed in 291.88s` |
| Independent real task (Fibonacci) via `python -m orchestrator task run` | PASS | `status=completed exitCode=0 durationMs=483141`. Artifacts `fibonacci.py`, `test_fibonacci.py`. 5 tool calls (1 intentional failing pytest run, then fixed). Independent sandbox re-run: `6 passed`. `fib(50)=12586269025`; -1→ValueError; 1.0/True/'3'→TypeError. Container removed, workspace kept. |
| Effective container security (live `docker inspect`, `docker exec`) | PASS | See Security Findings. |
| Path traversal via task/workspace IDs | PASS | All malicious IDs rejected by `validate_id`. The CLI prints a traceback instead of an error (P3-003). |
| R1: container removal fails | FAIL | Task stuck in `CLEANUP`; no event or result field records the failure; `stream_events` did not terminate within 10 s (it never does); a reconcile from a fresh manager removed the container. |
| R2: CLI `task reconcile` (no `--wait`) after SIGKILL of the owner | FAIL | Adopted task ended `cancelled` / "cancelled by orchestrator". |
| R3: adopt a relay task that exited while the orchestrator was down | FAIL (minor) | Task completed, but `/tmp/mapoc-f2nzlw7r` was left behind. |
| R4: idle connections to the relay socket | FAIL | Host threads 2 → 302 after 300 idle connections, still 302 after 20 s. |
| R5: reconcile from root B while a task of root A is live | FAIL | B: `removed: ['c97ad54da9fa']`; A's task ended `failed/infrastructure/container_lost`. |
| Host container set before vs after the whole review | PASS | Identical; 0 POC-labelled containers remain. |

Environment for every row: this host (uid 1000, docker group), repo `.venv` Python 3.12.11, Docker 28.5.0, Ollama with
`qwen3-vl:8b`. Probe scripts: `~/.copilot/session-state/<session>/files/review/r1..r5_*.py`.

## Detailed Findings

### P1-001 — `reconcile()` removes live task containers owned by orchestrators using another workspace root
- **Location:** `orchestrator/tasks.py:829` and `:835` (`RunLease.is_active(self.orch.state_dir, run_id)`, then
  `_safe_remove` when there is no record). Same pattern at `:853`. The CLI default root comes from
  `orchestrator/__main__.py:72`, a CWD-relative `workspaces`.
- **Evidence:** R5. A live `TaskManager` on `/tmp/p2review/r5-A` runs task `victim`; `TaskManager('/tmp/p2review/r5-B').reconcile()`
  returns `removed: ['c97ad54da9fa']`, and A's task ends `failed / container_lost`. This also happened unintentionally
  during the review: R1-R3 each removed one live container of the concurrently running e2e suite, failing tests B, C and
  D-infra.
- **Impact:** a routine operator action (`task reconcile` run from a different directory or with a different
  `MULTIAGENT_WORKSPACES`), or any second orchestrator instance, destroys in-flight agent work of other live orchestrators
  and records it as an infrastructure failure. The README claims that "Containers of live runs ... are respected"; that is
  false across roots. This blocks Phase 3, which will run several orchestrators or schedulers. The inherited Phase 1
  `cleanup_stale` (`manager.py:319`) has the same flaw for Phase 1 containers.
- **Recommended fix:** make run liveness independent of the caller's root. Use a single host-wide lease directory (for
  example `$XDG_RUNTIME_DIR/multiagent/runs`) for every run. Alternatively, add the owning workspace root as a container
  label and only consider containers whose label equals `self.root`, skipping all others. Never remove a container you
  have no record for unless it is provably owned by a dead run.
- **Verification:** extend `test_restart_reconciliation_adopts_only_owned_orphaned_tasks` with R5: a live task in root A,
  `reconcile()` from root B, then assert the container survives, A's task completes, and B reports it as
  skipped/ignored. Repeat for Phase 1 `cleanup_stale`.

### P2-001 — `task reconcile` without `--wait` cancels every task it adopts
- **Location:** `orchestrator/__main__.py:256-262` together with `TaskManager.close()` (`tasks.py:454-459`), which cancels
  all live tasks.
- **Evidence:** R2. The owner was SIGKILLed while the task was running; `main(["task","reconcile",...])` reports
  `adopted: ['r2']`, rc 0, and the task's final status is `cancelled` / "cancelled by orchestrator".
- **Impact:** the documented recovery command silently cancels the work it claims to have adopted, unless the operator
  happens to pass `--wait`. The task is then reported as a user cancellation.
- **Recommended fix:** in the CLI, either always wait for adopted tasks, or detach without cancelling: close the manager
  without cancelling adopted tasks and leave the container for the next reconcile. Make `close()` distinguish "shutdown
  and cancel" from "detach".
- **Verification:** run R2 again (SIGKILL the owner, then CLI reconcile without `--wait`) and assert that the task ends
  `completed`, or is still running and adoptable, never `cancelled`.

### P2-002 — Container removal failure is unrecorded and makes `task run` / `events --follow` hang forever
- **Location:** `tasks.py:787-797` (only `log.warning`; returns while still in CLEANUP) and `tasks.py:383-392`
  (`stream_events` exits only on `REMOVED`).
- **Evidence:** R1 (`docker rm` failure injected through a `DockerClient` subclass). State stays `CLEANUP`; the events end
  at `task.completed` with no cleanup event; `result.json` does not mention it; `stream_events` did not return. A
  reconcile from a new manager later removed the container, so the state is recoverable.
- **Impact:** the CLI used for real runs (`task run`) never terminates when the daemon fails to remove a container. The
  failure is visible only as a log warning, and nothing tells the operator to run reconcile.
- **Recommended fix:** emit a `container.cleanup_failed` event and record `cleanup: {removed: false, error}` in the
  task record and result. Make `stream_events` (and `wait()` for non-live tasks) also terminate once a result exists and
  the owner is done or the state is `CLEANUP`.
- **Verification:** R1 as a probe test. Assert the event exists, the cleanup error is recorded, `stream_events`
  terminates, and a later reconcile reaches `REMOVED`.

### P2-003 — An untrusted agent can exhaust host threads through the model relay
- **Location:** `orchestrator/llm_relay.py:29` (`ThreadingMixIn`, unbounded) and `:60-79` (handler has no socket timeout).
  Body reads use the client-supplied `Content-Length` (`:117-121`).
- **Evidence:** R4. 300 idle connections that send headers but no body give 300 host threads, and none time out after 20 s.
  Inside a task, the socket is reachable directly at `/run/llm/llm.sock` and through `127.0.0.1:11434`. The container's
  PID limit (512) does not bound host-side threads; only the container's file-descriptor limit does.
- **Impact:** a malicious or runaway agent can degrade or crash the orchestrator process, which supervises *all* tasks.
  It can also run an unbounded number of concurrent model requests on the shared GPU.
- **Recommended fix:** set `timeout` on the handler (for example 30 s idle, with the upstream timeout kept separately).
  Cap concurrent connections per relay with a semaphore (for example 4), refusing extra connections with 429. Reject
  negative or invalid `Content-Length`.
- **Verification:** R4 as a unit test. Assert that threads stay at or below the cap plus a constant, and that idle
  connections are closed after the timeout.

### P2-004 — The concurrency limit is per process, not per host
- **Location:** `tasks.py:265` (`threading.BoundedSemaphore`); the CLI creates one `TaskManager` per command.
- **Evidence:** static inspection. Each `python -m orchestrator task run` process gets its own semaphore (default 2), so
  N CLI invocations start 2N containers.
- **Impact:** there is no host-wide resource cap (CPU, memory, GPU); Phase 3 scheduling needs one. This is not a Phase 2
  functional failure, but it is a Phase 3 prerequisite.
- **Recommended fix:** a host-wide slot mechanism, such as N flock slot files in the same global lease directory as in
  P1-001, or a single long-running scheduler service.
- **Verification:** start 3 CLI tasks with `--max-concurrency 1` and assert that at most 1 container is running at a time.

### P3-001 — Relay temp directory leaks when adopting a task that already ended
- **Location:** `tasks.py:935-938`. `relay_tmp` is only set for `status == "running"`, so `_cleanup` (`:808`) never removes
  `relaySocketDir` for exited or terminal adoptions.
- **Evidence:** R3. The task completed after reconcile, but `/tmp/mapoc-f2nzlw7r` remained. (I removed it after the review.)
- **Recommended fix:** always remove `rec["relaySocketDir"]` during adoption cleanup, after checking it is a `mapoc-` directory.
- **Verification:** R3 asserts that no new `/tmp/mapoc-*` directories remain.

### P3-002 — Quota stop followed by a clean agent exit is reported as COMPLETED
- **Location:** `tasks.py:696-705`. Stop reasons are applied only when `failure is not None`.
- **Evidence:** static inspection. If the agent finishes cleanly during the SIGTERM grace period after a quota stop, the
  result is `completed` with an over-quota workspace. (For cancel or timeout, preferring a natural completion is defensible.)
- **Recommended fix:** treat `stop_reason == "quota"` as a failure regardless of the parser's verdict.
- **Verification:** a probe task that writes past the quota and then exits 0 on SIGTERM should end `failed/workspace_quota_exceeded`.

### P3-003 — Input errors surface as raw exceptions
- **Location:** `runtimes/pi.py:173,179` (`int(...)` on options) and `__main__.py:293`, which does not catch
  `WorkspaceError` or `ValueError`.
- **Evidence:** `options.contextWindow="big"` raises `ValueError: invalid literal for int()`. `task get ../../etc` prints a
  Python traceback, although it is correctly rejected.
- **Recommended fix:** validate runtime options inside `prepare` and raise `ValidationError`. Catch `WorkspaceError` in the CLI.
- **Verification:** unit tests for both cases asserting `ValidationError` and CLI exit code 2.

### P3-004 — Phase 1 and Phase 2 share the workspace-id namespace
- **Location:** `manager.py:555`. Phase 1 writes `workspace_root/<agent_id>/result.json`; Phase 2 bind-mounts
  `workspace_root/<workspace_id>` read-write into agents.
- **Impact:** with the same root (the default for both) and colliding IDs, a Phase 1 result lands inside an
  agent-writable task workspace, where an agent can read or tamper with it. Low likelihood.
- **Recommended fix:** use separate subdirectories, for example `workspaces/tasks/<id>` and `workspaces/agents/<id>`.
- **Verification:** a unit test with the same id used for both phases.

### P3-005 — Relay socket is world-connectable on the host
- **Location:** `llm_relay.py:55,81` (directory 0755 in `/tmp`, socket 0666).
- **Impact:** any local user can use the task's allowed model through it. This is equivalent to the direct Ollama access
  local users already have, so there is no privilege gain. Note it for multi-tenant Phase 3.
- **Recommended fix:** directory 0711, or group-restricted; or place it under the 0700 task directory and grant access
  only to the container uid, which is the same as the owner.
- **Verification:** `stat` the socket directory and socket during a run.

## Phase 1 Regression Assessment
No regression found. All 72 Phase 1 tests (15 lifecycle, 28 orchestrator, 13 isolation, 16 integration) pass on the
Phase 2 commit. The `ContainerSpec` defaults still produce the Phase 1 container (`test_pending_state_machine_and_phase1_defaults_unchanged`).
`cleanup_stale` now skips task containers and still removes plain orphans. The lease-scope flaw behind P1-001 already
exists in Phase 1 `cleanup_stale` (`manager.py:319`); it is inherited, not introduced.

## Phase 3 Readiness
The runtime adapter, the hardened container spec, the on-disk task records and the event model are a sound base for
multiple concurrent agent containers: two real agents already ran concurrently and isolated in test E. Before
multi-agent scheduling, the following must hold:
1. Run ownership must be host-global (P1-001).
2. Recovery must never cancel or destroy work it does not own (P2-001).
3. Capacity must be host-wide (P2-004).
4. The relay must be resource-bounded (P2-003), because several agents will share one orchestrator process and one GPU.

## Final Decision
**CHANGES REQUIRED**

The real-runtime integration is verified and of good quality, but P1-001 is a confirmed defect that destroys live task
work and makes restart reconciliation unsafe. Restart reconciliation is part of the Phase 2 scope and is a prerequisite
for Phase 3.

## Required Actions
1. **Fix P1-001 (mandatory).** Use host-global run liveness, or scope reconcile and stale cleanup to containers labelled
   with the caller's workspace root, for both Phase 2 `reconcile()` and Phase 1 `cleanup_stale()`. Add the R5 test.
2. **Fix P2-001.** The CLI `task reconcile` must not cancel adopted tasks. Add the R2 test.
3. **Fix P2-002.** Record cleanup failures as an event and in the result, and make `stream_events`/`wait` terminate.
   Add the R1 test.
4. **Fix P2-003.** Add handler timeouts and a connection cap to the relay. Add the R4 test.
5. Plan P2-004 (host-wide capacity) as the first Phase 3 item, or fix it now if it is cheap.
6. Optional: P3-001 to P3-005.
7. Re-run `pytest -m "not live"`, `pytest -m "live and not e2e"`, `pytest -m e2e` and the new R1/R2/R4/R5 tests. Then
   resubmit for review together with the test output.
