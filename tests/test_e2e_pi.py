"""Real end-to-end tests (Phase 2, Tests A-F): the real Pi coding agent runs inside the hardened container and
talks to a real local Ollama model through the orchestrator's relay. No mock agent is used for these tests.

Needs: Docker, the Pi image (built by the `pi_image` fixture) and the Ollama model below. Skipped otherwise.
Code the agent writes is only ever executed inside a throwaway hardened container, never on the host."""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import ROOT, docker_cli, project_containers
from orchestrator.runtimes import register
from orchestrator.runtimes.base import AgentRuntime
from orchestrator.runtimes.pi import PI_VERSION, PiRuntime
from orchestrator.tasks import TaskManager
from probe import ProbeParser

pytestmark = [pytest.mark.live, pytest.mark.e2e]

MODEL = os.environ.get("POC_E2E_MODEL", "qwen3-vl:8b")
HELLO = "Create a Python script named hello.py that prints 'Hello from Coding Agent'."


@pytest.fixture(scope="session")
def ollama_model():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as r:
            names = [m["name"] for m in json.load(r)["models"]]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Ollama not reachable: {exc}")
    if MODEL not in names:
        pytest.skip(f"Ollama model {MODEL} not installed")
    return MODEL


@pytest.fixture
def real(tm, ollama_model):
    return tm


def sandbox(workspace: Path, command: str, timeout=60):
    """Run a command against a task workspace in a throwaway hardened container (read-only mount)."""
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--read-only", "--user", f"{os.getuid()}:{os.getgid()}",
         "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true", "--pids-limit", "128", "--memory", "512m",
         "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=32m", "--mount", f"type=bind,src={workspace},dst=/workspace,readonly",
         "-e", "PYTHONDONTWRITEBYTECODE=1", "--workdir", "/workspace", "--entrypoint", "bash",
         "multiagent-poc-pi:phase2", "-c", command],
        capture_output=True, text=True, timeout=timeout)


def run_task(tm, request, timeout=900):
    tm.submit_task(request)
    snap = tm.wait(request["taskId"], timeout)
    assert snap["state"] == "REMOVED", f"task did not finish: {snap['state']}"
    return snap["result"]


def event_types(tm, tid):
    return [e["type"] for e in tm.get_task_events(tid)]


def gone(cid):
    return docker_cli("inspect", "--type", "container", cid, check=False).returncode != 0


# ---- Test A: runtime startup ------------------------------------------------------------------------------
class PiCliRuntime(AgentRuntime):
    """Runs a Pi CLI subcommand (e.g. --version) through the same container spec; no model call involved."""
    default_image = PiRuntime.default_image

    def __init__(self, name, args):
        self.name, self._args = name, args

    def prepare(self, req):
        plan = PiRuntime().prepare(req)
        plan.args = list(self._args)
        plan.files.pop("prompt.txt")
        return plan

    def new_parser(self):
        return ProbeParser()


def test_a_runtime_startup_pi_cli_runs_in_container_and_sees_configured_model(real, ollama_model):
    register(PiCliRuntime("pi-version", ["--version"]))
    register(PiCliRuntime("pi-models", ["--list-models"]))
    res = run_task(real, {"taskId": "e2e-a1", "runtime": "pi-version", "prompt": "-", "timeoutSeconds": 60})
    stdout = Path(res["logs"]["stdout"]).read_text()
    assert res["status"] == "completed" and res["exitCode"] == 0 and PI_VERSION in stdout, stdout
    res = run_task(real, {"taskId": "e2e-a2", "runtime": "pi-models", "prompt": "-", "model": ollama_model, "timeoutSeconds": 60})
    stdout = Path(res["logs"]["stdout"]).read_text()
    assert res["status"] == "completed" and "ollama" in stdout and ollama_model in stdout, stdout
    assert res["modelRelay"]["requests"] == 0  # initialisation needs no model traffic


# ---- Test B: basic coding task ----------------------------------------------------------------------------------
def test_b_basic_coding_task_creates_runs_and_preserves_hello_py(real, ollama_model):
    res = run_task(real, {"taskId": "e2e-b", "prompt": HELLO, "model": ollama_model, "timeoutSeconds": 600})
    assert res["status"] == "completed" and res["exitCode"] == 0 and res["error"] is None, json.dumps(res["error"])
    hello = real.root / "e2e-b" / "hello.py"
    assert hello.is_file() and "Hello from Coding Agent" in hello.read_text()
    assert [(a["path"], a["status"]) for a in res["artifacts"]] == [("hello.py", "added")]
    ran = sandbox(real.root / "e2e-b", "python3 hello.py")
    assert ran.returncode == 0 and ran.stdout.strip() == "Hello from Coding Agent", ran.stdout + ran.stderr
    assert "+print(" in Path(res["changes"]["diffFile"]).read_text()
    assert res["output"]["finalMessage"] and res["output"]["toolCalls"] >= 1 and res["output"]["usage"]["totalTokens"] > 0
    assert res["modelRelay"]["requests"] >= 2 and res["modelRelay"]["upstreamErrors"] == 0 and res["modelRelay"]["denied"] == 0
    types = event_types(real, "e2e-b")
    order = ["task.created", "container.starting", "container.started", "agent.starting", "agent.running",
             "agent.output", "agent.completed", "task.completed", "container.cleaned"]
    positions = [types.index(t) for t in order]
    assert positions == sorted(positions), types
    assert all(e["runtime"] == "pi" and e["taskId"] == "e2e-b" and e["timestamp"] for e in real.get_task_events("e2e-b"))
    assert gone(res["containerId"]) and real.get_task("e2e-b")["state"] == "REMOVED"
    assert "Hello from Coding Agent" in Path(res["logs"]["stdout"]).read_text()  # raw runtime output preserved
    assert real.get_task_result("e2e-b") == res  # still available after the container is gone


# ---- Test C: modify existing code, run existing tests -------------------------------------------------------------
BUGGY = "def add(a, b):\n    return a - b\n"
TESTS = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\ndef test_add_negative():\n    assert add(-1, 1) == 0\n"


def test_c_fix_bug_in_existing_code_and_tests_pass(real, ollama_model):
    wsdir = real.root / "e2e-c"
    wsdir.mkdir()
    (wsdir / "calc.py").write_text(BUGGY)
    (wsdir / "test_calc.py").write_text(TESTS)
    before = sandbox(wsdir, "python3 -m pytest -q -p no:cacheprovider")
    assert before.returncode != 0, "the seeded bug must make the tests fail before the agent runs"
    res = run_task(real, {
        "taskId": "e2e-c", "model": ollama_model, "timeoutSeconds": 780,
        "prompt": "The function add() in calc.py has a bug and the tests in test_calc.py fail. Fix the bug in calc.py, "
                  "then run the existing tests with `python3 -m pytest -q` and make sure they pass. Do not modify test_calc.py."})
    assert res["status"] == "completed", json.dumps(res["error"])
    assert (wsdir / "test_calc.py").read_text() == TESTS
    assert "return a - b" not in (wsdir / "calc.py").read_text()
    changed = {a["path"]: a["status"] for a in res["artifacts"]}
    assert changed.get("calc.py") == "modified" and "test_calc.py" not in changed
    diff = Path(res["changes"]["diffFile"]).read_text()
    assert "-    return a - b" in diff and "+    return a + b" in diff
    after = sandbox(wsdir, "python3 -m pytest -q -p no:cacheprovider")
    assert after.returncode == 0 and "2 passed" in after.stdout, after.stdout + after.stderr
    calls = [e["data"].get("args", "") for e in real.get_task_events("e2e-c") if e["data"].get("kind") == "tool_call"]
    assert any("pytest" in c for c in calls), f"agent never ran the tests: {calls}"


# ---- Test D: failure handling ---------------------------------------------------------------------------------------
def test_d_invalid_model_is_a_model_error_not_a_success(real):
    res = run_task(real, {"taskId": "e2e-d-model", "prompt": HELLO, "model": "does-not-exist:1b", "timeoutSeconds": 120})
    assert res["status"] == "failed" and res["error"]["kind"] == "agent_failure" and res["error"]["code"] == "model_error", res["error"]
    assert res["artifacts"] == [] and gone(res["containerId"])
    assert res["modelRelay"]["upstreamErrors"] >= 1


def test_d_missing_provider_credentials_are_an_auth_error(real):
    res = run_task(real, {"taskId": "e2e-d-auth", "prompt": HELLO, "provider": "anthropic", "model": "claude-sonnet-4-5",
                          "timeoutSeconds": 120})
    assert res["status"] == "failed" and res["error"]["kind"] == "agent_failure", res["error"]
    assert res["error"]["code"] == "auth_error", res["error"]
    assert gone(res["containerId"])


def test_d_agent_process_failure_is_an_agent_failure(real):
    res = run_task(real, {"taskId": "e2e-d-proc", "prompt": HELLO, "provider": "no-such-provider", "model": "x",
                          "timeoutSeconds": 120})
    assert res["status"] == "failed" and res["exitCode"] not in (0, None)
    assert res["error"]["kind"] == "agent_failure" and "no-such-provider" in res["error"]["message"], res["error"]
    assert gone(res["containerId"])


def test_d_unreachable_model_backend_is_an_infrastructure_failure(live_docker, pi_image, ollama_model, tmp_path):
    with TaskManager(docker=live_docker, workspace_root=tmp_path, upstream=("127.0.0.1", 1)) as m:
        res = run_task(m, {"taskId": "e2e-d-infra", "prompt": HELLO, "model": ollama_model, "timeoutSeconds": 180})
    assert res["status"] == "failed" and res["error"]["kind"] == "infrastructure", res["error"]
    assert res["error"]["code"] == "llm_backend_unreachable"


def test_d_real_agent_timeout_terminates_the_container(real, ollama_model):
    real.submit_task({"taskId": "e2e-d-timeout", "prompt": HELLO + " Then explain in detail how it works.",
                      "model": ollama_model, "timeoutSeconds": 6})
    cid = until_running(real, "e2e-d-timeout")
    t0 = time.time()
    snap = real.wait("e2e-d-timeout", 120)
    res = snap["result"]
    assert res["status"] == "timed_out" and res["error"]["kind"] == "timeout" and snap["state"] == "REMOVED"
    assert time.time() - t0 < 30 and gone(cid)


def test_d_real_agent_cancellation(real, ollama_model):
    real.submit_task({"taskId": "e2e-d-cancel", "prompt": HELLO, "model": ollama_model, "timeoutSeconds": 600})
    cid = until_running(real, "e2e-d-cancel")
    until_event(real, "e2e-d-cancel", "agent.running")
    t0 = time.time()
    real.cancel_task("e2e-d-cancel")
    res = real.wait("e2e-d-cancel", 120)["result"]
    assert res["status"] == "cancelled" and res["error"]["kind"] == "cancelled" and gone(cid)
    assert time.time() - t0 < 30
    assert "task.cancelled" in event_types(real, "e2e-d-cancel")


def until_running(tm, tid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        t = tm.get_task(tid)
        if t["state"] == "RUNNING" and t["containerId"]:
            return t["containerId"]
        time.sleep(0.1)
    raise AssertionError(f"{tid} never reached RUNNING")


def until_event(tm, tid, etype, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if etype in event_types(tm, tid):
            return
        time.sleep(0.2)
    raise AssertionError(f"{tid}: no {etype} event")


# ---- Test E: isolation with two real agents --------------------------------------------------------------------------
def test_e_two_real_agents_stay_in_their_own_workspaces(real, ollama_model):
    real.submit_task({"taskId": "e2e-e1", "model": ollama_model, "timeoutSeconds": 900, "prompt":
                      "Do two things. First create a file named one.txt containing exactly the text ONE. Second, use the "
                      "bash tool to run exactly this command: (id; ls -la /; ls /var/run; env) > report.txt 2>&1"})
    real.submit_task({"taskId": "e2e-e2", "model": ollama_model, "timeoutSeconds": 900, "prompt":
                      "Do two things. First create a file named two.txt containing exactly the text TWO. Second, use the "
                      "bash tool to run exactly this command: find / -name 'one.txt' -not -path '/proc/*' > found.txt 2>/dev/null"})
    r1, r2 = real.wait("e2e-e1", 900)["result"], real.wait("e2e-e2", 900)["result"]
    assert r1["status"] == "completed" and r2["status"] == "completed", (r1["error"], r2["error"])
    w1, w2 = real.root / "e2e-e1", real.root / "e2e-e2"
    assert (w1 / "one.txt").read_text().strip() == "ONE" and (w2 / "two.txt").read_text().strip() == "TWO"
    assert not (w1 / "two.txt").exists() and not (w2 / "one.txt").exists()
    assert r1["containerId"] != r2["containerId"]
    # The runs overlapped in time (both were live at once), so the isolation above is not a sequencing artefact.
    assert r1["startedAt"] < r2["finishedAt"] and r2["startedAt"] < r1["finishedAt"]
    report = (w1 / "report.txt").read_text() if (w1 / "report.txt").exists() else ""
    assert f"uid={os.getuid()}" in report, f"agent 1 did not run the probe command: {report[:200]!r}"
    assert "uid=0" not in report and "docker.sock" not in report and "ANTHROPIC" not in report and "celltester" not in report
    found = (w2 / "found.txt").read_text() if (w2 / "found.txt").exists() else None
    assert found is not None, "agent 2 did not run the search command"
    assert "e2e-e1" not in found and found.strip() == "", f"agent 2 can see agent 1's files: {found!r}"


# ---- Test F: restart recovery with a real agent ------------------------------------------------------------------------
def test_f_orchestrator_restart_during_a_real_task_is_reconciled(live_docker, pi_image, ollama_model, tmp_path):
    root = tmp_path / "ws"
    req = {"taskId": "e2e-f", "prompt": HELLO, "model": ollama_model, "timeoutSeconds": 600, "runtime": "pi"}
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "hold_task.py"), str(root), json.dumps(req)],
                            stdout=subprocess.PIPE, text=True)
    tid, dead_run, cid = proc.stdout.readline().split()
    try:
        proc.send_signal(signal.SIGKILL)  # unexpected restart while Pi is working
        proc.wait(10)
        assert docker_cli("inspect", "--format", "{{.State.Running}}", cid).stdout.strip() == "true"
        with TaskManager(docker=live_docker, workspace_root=root) as recovery:
            report = recovery.reconcile()
            assert report["adopted"] == ["e2e-f"], report
            snap = recovery.wait("e2e-f", 600)
            res = snap["result"]
            types = event_types(recovery, "e2e-f")
            assert "task.reconciled" in types and types[-1] == "container.cleaned" and snap["state"] == "REMOVED"
            assert res["status"] == "completed", (res["status"], res["error"])
            assert "Hello from Coding Agent" in (root / "e2e-f" / "hello.py").read_text()
            assert gone(cid)
            seqs = [e["seq"] for e in recovery.get_task_events("e2e-f")]
            assert seqs == list(range(1, len(seqs) + 1)), "event sequence must stay gap-free and duplicate-free across the restart"
        assert project_containers(recovery.run_id) == []
    finally:
        if proc.poll() is None:
            proc.kill()
        docker_cli("rm", "-f", cid, check=False)
