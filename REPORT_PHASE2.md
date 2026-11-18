# Phase 2 Report — Real Coding Agent Runtime Integration

Date: 2026-11-18. Runtime: **Pi coding agent 1.5.2** (`@earendil-works/pi-coding-agent`) with a local **Ollama** model (`qwen3-vl:8b`).
Status: Phase 2 Definition of Done met for the local-model path; remote-provider (internet) path is **blocked by design** (see section 9).

## 0. Integration assessment (initial inspection)

| Item | Finding |
|---|---|
| Phase 1 baseline | Python stdlib orchestrator (`manager.py` Orchestrator, `lifecycle.py` StateMachine + flock `RunLease`, `docker_client.py` hardened `docker create`). 72/72 tests passed before any change. |
| Agent CLIs on host | `pi` 1.5.2 (managed install under `~/.pi/agent/install`), `claude` (Claude Code). Node 20 on PATH; Pi's bundled Node is 22.23. |
| Credentials found | Pi default provider is `github-copilot` with an OAuth login in `~/.pi/agent/auth.json`; Claude Code has its own login. Only key **names**/file existence were inspected; no values were read or printed. **None of it is used or copied.** |
| Model backends | Ollama 0.32 on `127.0.0.1:11434` (`qwen3-vl:4b/8b/30b`, all advertise `tools`); 2x RTX A2000 12 GB. |
| Selection | **Pi + local Ollama.** Pi has a documented non-interactive JSONL mode (`--mode json`), supports custom OpenAI-compatible providers (Ollama), is already installed (same version used, pinned in the image), and needs no credential for a local model. Claude Code was not chosen: it requires Anthropic auth and internet. |
| Integration points | `ContainerSpec`/`build_create_args` (extended, defaults unchanged), `StateMachine` (+`PREPARING`), `RunLease` (task/workspace ownership), `Orchestrator.cleanup_stale` (now skips task containers), `DockerClient`. |

No agent software was installed or upgraded on the host. Pi is installed **inside the image** at build time from the host's own lockfile.

## 1. Architecture and integration changes

```text
CLI / TaskManager API -> TaskManager -> AgentRuntime adapter (prepare / parse / classify)
                            |                 -> Pi: `pi --mode json --provider ollama --model <m> ...`
                            +-> Docker container (Phase 1 hardening)           workspaces/<id> (bind rw) -> /workspace
                            +-> LLM relay (host, unix socket, allow-list) ---> Ollama 127.0.0.1:11434
                            +-> .tasks/<id>/ (task.json events.jsonl stdout.log stderr.log result.json changes.diff)
```

Changes to Phase 1 (all additive; Phase 1 defaults reproduce the old container exactly, verified by tests):

* `lifecycle.py`: new `PREPARING` state (`CREATED -> PREPARING -> STARTING`); Phase 1 transitions unchanged.
* `models.py` / `docker_client.py`: `ContainerSpec` gained optional `mounts`, `tmpfs`, `args`, `user`, `entrypoint`, `workspace_tmpfs`, `log_max_size`, `stop_timeout_s`; `build_create_args` still hard-codes `--network none`, read-only rootfs, `--cap-drop ALL`, `no-new-privileges`, limits; bind-mount paths with `,`/quotes/relative are refused. Added `follow_logs`, `image_label`, `build_image(build_args)`.
* `manager.py`: `cleanup_stale` skips containers labelled `multiagent.poc.kind=agent-task` (they are reconciled, not blindly removed).
* `__main__.py`: `build --runtime pi` and the `task` subcommands.

## 2. Selected runtime and version

Pi **1.5.2** (commit `b4afdddf` per Pi's metadata), pinned by `agent/pi/package-lock.json` copied from the host's managed install (`npm ci --ignore-scripts`). Base `node:22-bookworm-slim` + `bash git ripgrep fd-find python3 python3-pytest`. Model: `qwen3-vl:8b` (Q4_K_M, 32768-token context confirmed via `ollama ps`).

## 3. Files created or modified

Created: `agent/pi/{Dockerfile,entrypoint.sh,llm-relay.js,package.json,package-lock.json}`, `orchestrator/{tasks.py,workspace.py,llm_relay.py}`, `orchestrator/runtimes/{__init__,base,pi}.py`, `tests/{test_runtime_units,test_tasks_probe,test_e2e_pi}.py`, `tests/{probe,hold_task}.py`, `REPORT_PHASE2.md`.
Modified: `orchestrator/{__main__,docker_client,lifecycle,manager,models}.py`, `tests/conftest.py`, `pytest.ini`, `README.md`.

## 4. Container image and configuration

* Image `multiagent-poc-pi:phase2`, built for the caller's uid (`python -m orchestrator build --runtime pi`); label `multiagent.poc.agent_uid` is verified at task start (`image_uid_mismatch` otherwise). `PI_OFFLINE=1`, `PI_SKIP_VERSION_CHECK=1`, `PI_TELEMETRY=0`.
* Per task container: user `<host uid>:<host gid>` (non-root; refuses to run as uid 0 by falling back to 10001), `--network none`, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, default seccomp, `--init`, **2 CPUs / 2 GiB (no swap) / 512 PIDs** (configurable per task), tmpfs `/tmp` (256 MB) and `/home/agent` (128 MB, agent state), mounts limited to: `/workspace` (rw, this task only), `/run/task` (ro), optional `/run/secrets` (ro), optional `/run/llm` (ro socket dir). No Docker socket, no home, no host PID/IPC/network, not privileged.
* Pi is started with `--no-session --no-extensions --no-skills --no-mcp --no-themes --no-prompt-templates --thinking off`; the prompt is passed from a file (`/run/task/prompt.txt`), not argv.

## 5. Example task execution (real, CLI)

```text
$ python -m orchestrator task run --prompt "Create a Python script named hello.py that prints 'Hello from Coding Agent'." --task-id task-001 --timeout 600
19:54:11.450 task.created   19:54:11.486 container.starting   19:54:11.659 container.started   19:54:12.151 agent.running
19:54:37.145 agent.output  tool_call write {"path":"hello.py", ...}   19:54:37.148 tool_result "Successfully wrote to /workspace/hello.py"
19:54:54.849 agent.output  assistant_message "The script ... has been created successfully"   19:54:55.059 agent.completed
19:54:55.059 task.completed   19:54:55.075 container.cleaned
-> status completed, exitCode 0, durationMs 43399, artifacts [hello.py added 32 bytes], tokens in/out 3065/1038, container removed, workspaces/task-001/hello.py kept
```

## 6. Tests and actual results

Commands: `pytest -m "not live"` (unit), `pytest -m "live and not e2e"` (real Docker, no LLM), `pytest -m e2e` (real Pi + real model), and the full `pytest -v`.

Final full run (`pytest -v`, 6 min 15 s): **145 passed, 0 failed, 0 skipped.** Host container set identical before/after, 0 labelled containers, 0 leaked relay temp dirs, the 8 unrelated production containers untouched.

| File | Kind | Tests | Result |
|---|---|---|---|
| `test_lifecycle.py`, `test_orchestrator.py` (Phase 1 unit) | unit, fake Docker (logic only) | 15 + 28 | PASS |
| `test_isolation.py`, `test_integration.py` (Phase 1 live) | real Docker, mock agent | 13 + 16 | PASS (Phase 1 unchanged) |
| `test_runtime_units.py` | unit: request validation, redaction, Pi output classification (real message shapes), workspace diff/symlink/FIFO safety, relay allow-list against a fake upstream, container-spec invariants, Phase 1 sweep skips tasks | 46 | PASS |
| `test_tasks_probe.py` | **real Docker**, deterministic probe runtime through the real TaskManager (no LLM) | 16 | PASS |
| `test_e2e_pi.py` | **real Pi 1.5.2 + real Ollama model** | 11 | PASS |

Real end-to-end (Tests A-F, no mock agent):

| Spec test | What ran | Result |
|---|---|---|
| A Runtime startup | Pi `--version` -> `1.5.2` and `--list-models` -> `ollama qwen3-vl:8b` inside the hardened task container (0 model requests) | PASS |
| B Basic coding task | Pi wrote `hello.py`; executed in a throwaway hardened container -> `Hello from Coding Agent`; artifact `hello.py added`; diff saved; events in the required order; container removed; result still readable | PASS |
| C Modify existing code | Seeded `a - b` bug; tests failed before; Pi fixed `calc.py` only, ran `pytest` (verified in tool-call events); tests `2 passed` after; `test_calc.py` byte-identical; diff shows the exact change | PASS |
| D Failure handling | invalid model -> `agent_failure/model_error` (Pi exit 0, Ollama 404); missing credentials -> `auth_error` (Pi: "No API key found for anthropic") and `startup_failure/missing_credentials` (orchestrator, no container created); agent process failure (unknown provider, exit 1) -> `agent_error`; startup failure (`docker start` fails) -> `startup_failure`; backend down -> `infrastructure/llm_backend_unreachable`; docker down -> `docker_unavailable`; missing/unfit image -> `image_missing`/`image_uid_mismatch`; real-agent timeout (6 s) -> `timed_out`; real-agent cancel -> `cancelled` (SIGTERM, exit 143). Every case ends `REMOVED` with the container gone | PASS |
| E Isolation | two **real** Pi agents ran concurrently (overlapping run intervals); each workspace holds only its own file; agent 1's `id/ls/env` report shows host uid, no `docker.sock`, no host home or host env; agent 2's `find / -name one.txt` found nothing. Deterministic probes (`docker exec` into real task containers): uid, 0 capabilities, `NoNewPrivs`, seccomp, read-only rootfs, only `lo`, no socket/home, other workspace unreachable, symlink/FIFO attack harmless on the host | PASS |
| F Restart recovery | orchestrator process **SIGKILLed** while Pi was working; new `TaskManager.reconcile()` adopted the still-running container, restarted its model relay, Pi finished, result `completed`, `hello.py` present, event `seq` gap-free, container removed. Probe variants: original 25 s deadline honoured across the restart; task that finished while the orchestrator was down is collected; stale task container with no record removed; container of a **live** orchestrator, unlabelled and project-label-only containers preserved; Phase 1 `cleanup --stale` does not remove the recoverable task | PASS |

Also covered by probe tests: forced SIGKILL after a SIGTERM-ignoring agent, cross-process cancel, duplicate task id (idempotent / conflict), busy-workspace rejection and reuse, workspace quota enforcement (`workspace_quota_exceeded`), bounded output under a 3 MB flood, credential files (0400, read-only mount, not in `docker inspect`, redacted from logs/events/result, deleted after the run).

### Definition of Done checklist

| Item | Status | Evidence |
|---|---|---|
| Real coding-agent CLI runs in an isolated container | **Tested** | Test A/B: Pi 1.5.2 `--version`, `--list-models`, full coding run |
| Launched through the runtime adapter | **Tested** | `PiRuntime.prepare`/`PiParser` used by every Pi task; probe + Pi-CLI test runtimes use the same interface |
| Natural-language task produces a real source file | **Tested** | B: `hello.py` created; executed in a throwaway sandbox container -> `Hello from Coding Agent` |
| Workspace persists after container removal | **Tested** | B, C, E, probe isolation test |
| Status and logs available | **Tested** | `get_task`, `get_task_events`, `stdout.log`, `result.json` read after `REMOVED`; CLI `get/events/result/list` |
| Cancellation and timeouts | **Tested** | probe (timeout, cooperative cancel, SIGKILL fallback, cross-process cancel) + real-agent timeout and cancel |
| Agent vs infrastructure failures distinguishable | **Tested** | D: `model_error`, `auth_error`, `agent_error`, `startup_failure`, `llm_backend_unreachable`, `docker_unavailable`, `image_missing`, `image_uid_mismatch`, timeout, cancelled |
| Workspace isolation | **Tested** | E (two real concurrent agents) + deterministic probe isolation tests; symlink/FIFO attack test |
| No privileged config / no docker socket | **Tested** | `docker inspect` assertions on live task containers; mutation check (below) |
| Phase 1 tests still pass | **Tested** | all 72 Phase 1 tests pass in the final full run |
| Real E2E results documented | **This report** | |

Non-vacuity check: three deliberate weakenings of the task container spec (run as root; mount the host home; mount the whole workspace root) each made the isolation test fail; the code was restored afterwards.

### Defects found by the tests and fixed

* `remove_tree` could not delete directories an agent had `chmod 000`'d (caught by a unit test); now repairs permissions first.
* Redaction pattern `sk-...` matched inside `task-...` container names; now word-bounded.
* Pi exits 0 after a model error (and with no prompt): success now requires a completed Pi run with no error stop reason.
* First `task` run failed because the image predated the entrypoint's prompt-file support (stale image); the orchestrator correctly reported `failed/no_result` instead of success.
* Test E's first version assumed the 8B model would execute a loosely worded request; the model wrote the literal command text to the file. This was model non-compliance, not an isolation failure; the prompt now gives an explicit command and the test requires evidence the command ran (the assertion was not weakened).

## 7. Resource usage (measured on the example run)

Sampled with `docker stats` once per second during the real CLI task: peak memory **683 MiB** (limit 2048), peak CPU **10 %** (limit 200 %), peak PIDs **42** (limit 512). Inference runs in Ollama on the host GPU (11 GB VRAM for the 8B model with a 32k context), not in the container. Cold model load adds about 30-50 s to the first task; later tasks start generating within seconds. Image size and build time were not measured.

## 8. Known limitations and security risks

* **Docker is a basic isolation boundary**, not hardened: shared kernel, not rootless, no userns remap/gVisor. Anyone who can run the orchestrator effectively has host root via Docker.
* **Credentials accessible to the agent process** (when `credentials` is used) are readable by the agent and any code it runs. Redaction is best-effort (exact values plus common token shapes). The default local-Ollama path uses no credential and never exposes Pi's personal Copilot login.
* **Model relay**: the agent can use the configured model freely (compute abuse, no rate limit); prompts/responses pass through the orchestrator process. Only the allow-listed routes/model are reachable, and only 127.0.0.1:11434.
* **Workspace is untrusted output.** It is bind-mounted rw and owned by your uid: do not run its files, hooks (`.git/hooks`) or symlinks on the host without inspection. The orchestrator itself never executes, follows symlinks in, or runs `git` in it.
* **Disk quota is soft** (checked every 2 s); a fast writer can overshoot before being stopped. No per-task CPU-time budget or token budget.
* **Logs contain the full conversation** (Pi echoes system prompt, files it reads). They are redacted for known secrets only and kept until `purge_task`.
* Restart recovery restarts the relay for the adopted task; an in-flight model request is lost and relies on Pi's own retry (it worked in test F, but is timing-dependent). The adopted task keeps its original deadline.
* A small local model (8B) is non-deterministic: it passed B, C, E and F but E failed once on a badly worded prompt. Expect occasional agent-level failures; the orchestrator classifies them, it cannot prevent them.
* The in-container uid equals the host uid, so the Pi image is per-user. Running the orchestrator as root is only lightly handled (falls back to uid 10001 with `chown`; **untested**).
* Single-host, in-memory + on-disk state; no queueing beyond a semaphore; the one-task-per-workspace rule is enforced by flock files.
* Container startup failure is tested by wrapping the real Docker client to fail `docker start` (the container is really created and then really removed); a genuine daemon-side start failure was not provoked.

## 9. Implemented / tested / untested / blocked

| Area | State |
|---|---|
| Runtime adapter interface + Pi adapter, TaskManager API, events, results, redaction, workspace diff, quota, reconcile, CLI | Implemented **and tested** |
| Local-model path (Ollama via relay) with real Pi | Implemented and tested end to end |
| Remote LLM providers with credentials (Anthropic/OpenAI/...) | Credential *handling* implemented and tested with the probe runtime; the provider call itself is **blocked**: it needs allow-listed egress, which is not implemented (`network` only accepts `none`) and was not weakened. A real Pi run against such a provider was **not** performed. |
| Running the orchestrator as root, multi-user hosts, Windows/macOS | Untested |
| Very large workspaces (>20k files / >64 MB baseline) | Bounded by caps; untested beyond the unit scale |
| 30B model / long-running (>15 min) tasks, token budgets | Untested |

## 10. Remaining work for Phase 3

1. Allow-listed egress (proxy container or host proxy with host-side credential injection so the agent never sees the key) to enable hosted models.
2. Stronger isolation: rootless Docker or gVisor/Kata, user-namespace remap (removes the per-uid image), hard disk quotas (project quotas or per-task loop/xfs volumes).
3. Persistent task store (SQLite) + queue with priorities/backoff/rate limits, per-task token/CPU budgets, structured metrics, retention/purge policy for `.tasks`.
4. HTTP/SSE API in front of `TaskManager` (the Python/CLI API is the contract today), authentication, multi-tenant workspaces.
5. Artifact export (tarball/Git patch applied in a clean clone) and optional git-aware workflows done in a sandbox, never on the host.
6. Multi-agent collaboration / agent-to-agent messaging (explicitly out of scope for this phase).
