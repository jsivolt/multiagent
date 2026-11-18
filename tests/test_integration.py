"""Live Docker integration tests (real containers): creation, execution, concurrency,
failure isolation, timeouts, orphan recovery, repeated execution, CLI."""
import json
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from conftest import ROOT, docker_cli, project_containers
from orchestrator import Orchestrator, TaskConfig
from orchestrator.models import DEFAULT_IMAGE, LABEL_PROJECT, LABEL_RUN_ID

pytestmark = pytest.mark.live


def container_exists(cid):
    return docker_cli("inspect", "--type", "container", cid, check=False).returncode == 0


def max_overlap(results):
    from orchestrator.__main__ import _max_overlap

    return _max_overlap(results)


# Test 1 -------------------------------------------------------------------------------
def test_dynamic_creation_unique_ids_image_limits_labels(orch):
    ids = [orch.create_agent(TaskConfig(type="echo")) for _ in range(3)]
    infos = [docker_cli("inspect", orch.get_agent_status(i)["container_id"]).stdout for i in ids]
    docs = [json.loads(i)[0] for i in infos]
    assert len({d["Id"] for d in docs}) == 3 and len(set(ids)) == 3
    for d, agent_id in zip(docs, ids):
        assert d["Config"]["Image"] == DEFAULT_IMAGE
        assert d["State"]["Status"] == "created"
        labels = d["Config"]["Labels"]
        assert labels[LABEL_PROJECT] == "phase1" and labels[LABEL_RUN_ID] == orch.run_id
        assert labels["multiagent.poc.agent_id"] == agent_id
        hc = d["HostConfig"]
        assert (hc["NanoCpus"], hc["Memory"], hc["MemorySwap"], hc["PidsLimit"]) == (
            500_000_000, 256 * 1024**2, 256 * 1024**2, 64)
    for agent_id in ids:
        orch.destroy_agent(agent_id)
        assert orch.get_agent_status(agent_id)["state"] == "REMOVED"


# Test 2 -------------------------------------------------------------------------------
def test_echo_success_structured_result_and_container_removed(orch):
    agent_id = orch.create_agent(TaskConfig(type="echo", params={"message": "Hello Agent"}, task_id="task-001"))
    orch.start_agent(agent_id)
    assert orch.get_agent_status(agent_id)["state"] == "RUNNING"
    result = orch.collect_agent_result(agent_id)
    cid = result.container_id
    assert container_exists(cid), "container must still exist until explicitly destroyed"
    orch.destroy_agent(agent_id)
    assert not container_exists(cid)
    assert (result.task_id, result.agent_id, result.status, result.exit_code) == (
        "task-001", agent_id, "completed", 0)
    assert result.output == {"message": "Hello Agent"} and result.error is None
    assert result.duration_ms is not None and 0 < result.duration_ms < 30_000
    saved = json.loads((orch.workspace_root / agent_id / "result.json").read_text())
    assert saved == result.to_dict()
    assert orch.get_agent_status(agent_id)["state"] == "REMOVED"


def test_compute_task_is_correct(orch):
    r = orch.run_agent(TaskConfig(type="compute", params={"n": 100}))
    assert r.status == "completed" and r.output == {"n": 100, "primes_up_to_n": 25}


def test_invalid_task_params_fail_with_app_failure(orch):
    r = orch.run_agent(TaskConfig(type="sleep", params={"duration": "soon"}))
    assert r.status == "failed" and r.exit_code == 2 and r.error["kind"] == "app_failure"


def test_exit_zero_without_agent_result_is_not_success(orch):
    """A container that starts and exits 0 but is not our agent must not be reported as success."""
    r = orch.run_agent(TaskConfig(type="echo", image="python:3.12-alpine"))
    assert r.exit_code == 0 and r.status == "failed" and r.error["kind"] == "app_failure"


def test_missing_image_is_infrastructure_failure(orch):
    r = orch.run_agent(TaskConfig(type="echo", image="multiagent-poc-no-such-image:0"))
    assert r.status == "failed" and r.error["kind"] == "infrastructure"


def test_cancellation_stops_real_container(orch):
    agent_id = orch.create_agent(TaskConfig(type="sleep", params={"duration": 60}))
    orch.start_agent(agent_id)
    cid = orch.get_agent_status(agent_id)["container_id"]
    t0 = time.monotonic()
    orch.stop_agent(agent_id)
    assert time.monotonic() - t0 < 10
    assert orch.collect_agent_result(agent_id).status == "cancelled"
    assert json.loads(docker_cli("inspect", cid).stdout)[0]["State"]["Running"] is False
    orch.destroy_agent(agent_id)
    assert not container_exists(cid)


# Test 3 -------------------------------------------------------------------------------
def test_parallel_execution_overlaps(orch):
    t0 = time.monotonic()
    results = orch.run_many([TaskConfig(type="sleep", params={"duration": 2}) for _ in range(3)])
    wall = time.monotonic() - t0
    assert [r.status for r in results] == ["completed"] * 3
    assert len({r.container_id for r in results}) == 3
    assert max_overlap(results) == 3
    assert wall < 5.5, f"3x2s tasks took {wall:.1f}s: not concurrent"


def test_concurrency_limit_enforced_on_real_docker(live_docker, tmp_path):
    with Orchestrator(docker=live_docker, workspace_root=tmp_path, max_concurrency=2) as o:
        peak, stop = 0, threading.Event()

        def sample():
            nonlocal peak
            while not stop.is_set():
                running = docker_cli("ps", "-q", "--filter", f"label={LABEL_RUN_ID}={o.run_id}",
                                     "--filter", "status=running").stdout.split()
                peak = max(peak, len(running))
                time.sleep(0.05)

        sampler = threading.Thread(target=sample)
        sampler.start()
        try:
            results = o.run_many([TaskConfig(type="sleep", params={"duration": 1.5}) for _ in range(5)])
        finally:
            stop.set()
            sampler.join()
        assert [r.status for r in results] == ["completed"] * 5
        assert peak == 2, f"docker ps observed {peak} concurrent containers, expected exactly 2"
        assert max_overlap(results) == 2


# Test 4 -------------------------------------------------------------------------------
def test_failure_isolation(orch):
    results = orch.run_many([
        TaskConfig(type="sleep", params={"duration": 1.5}),
        TaskConfig(type="fail", params={"message": "boom", "exit_code": 7}),
        TaskConfig(type="sleep", params={"duration": 1.5}),
    ])
    ok1, bad, ok2 = results
    assert ok1.status == ok2.status == "completed" and ok1.exit_code == ok2.exit_code == 0
    assert (bad.status, bad.exit_code) == ("failed", 7)
    assert bad.error["kind"] == "app_failure" and bad.error["message"] == "boom"
    assert project_containers(orch.run_id) == []


# Test 5 -------------------------------------------------------------------------------
def test_timeout_kills_real_container_and_leaves_no_process(orch):
    agent_id = orch.create_agent(TaskConfig(type="sleep", params={"duration": 60}, timeout_s=3))
    orch.start_agent(agent_id)
    cid = orch.get_agent_status(agent_id)["container_id"]
    host_pid = json.loads(docker_cli("inspect", cid).stdout)[0]["State"]["Pid"]
    assert host_pid > 0 and Path(f"/proc/{host_pid}").exists()
    t0 = time.monotonic()
    result = orch.collect_agent_result(agent_id)
    elapsed = time.monotonic() - t0
    assert result.status == "timed_out" and result.error["kind"] == "timeout"
    assert result.exit_code == 137  # SIGKILL
    assert 2.5 < elapsed < 8, f"timeout took {elapsed:.1f}s"
    assert not Path(f"/proc/{host_pid}").exists(), "container process still alive after timeout"
    orch.destroy_agent(agent_id)
    assert not container_exists(cid)


# Test 8 -------------------------------------------------------------------------------
def _spawn_holder(workspace_root):
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "hold_run.py"), str(workspace_root)],
                            stdout=subprocess.PIPE, text=True)
    run_id, cid = proc.stdout.readline().split()
    return proc, run_id, cid


def _running(cid):
    r = docker_cli("inspect", "--format", "{{.State.Running}}", cid, check=False)
    return r.returncode == 0 and r.stdout.strip() == "true"


def test_orphan_recovery_removes_only_owned_stale_containers(live_docker, tmp_path):
    root = tmp_path / "ws"
    baseline = set(docker_cli("ps", "-aq", "--no-trunc").stdout.split())
    decoys, procs = [], []

    def decoy(*labels):
        args = ["create", "--name", f"poc-test-decoy-{uuid.uuid4().hex[:6]}", "--network", "none",
                "--entrypoint", "sleep"]
        for label in labels:
            args += ["--label", label]
        cid = docker_cli(*args, DEFAULT_IMAGE, "300").stdout.strip()
        docker_cli("start", cid)
        decoys.append(cid)
        return cid

    try:
        unlabeled = decoy()
        project_only = decoy(f"{LABEL_PROJECT}=phase1")  # shares the project label, no run id
        dead_run_no_lease = decoy(f"{LABEL_PROJECT}=phase1", f"{LABEL_RUN_ID}=ghost-run")
        stale_proc, stale_run, stale_cid = _spawn_holder(root)
        procs.append(stale_proc)
        active_proc, active_run, active_cid = _spawn_holder(root)
        procs.append(active_proc)

        stale_proc.send_signal(signal.SIGKILL)  # interrupted run: container is orphaned
        stale_proc.wait(10)
        assert _running(stale_cid), "orphan should still be running before recovery"

        with Orchestrator(docker=live_docker, workspace_root=root) as recovery:
            managed = {r["name"]: r["owner"] for r in recovery.list_managed()}
            report = recovery.cleanup_stale()

        assert set(report["removed_containers"]) == {stale_cid, dead_run_no_lease}
        assert active_run in report["skipped_active_runs"]
        assert not container_exists(stale_cid) and not container_exists(dead_run_no_lease)
        assert _running(active_cid), "container of an active run must be preserved"
        assert _running(unlabeled) and _running(project_only), "unowned containers must be preserved"
        assert baseline <= set(docker_cli("ps", "-aq", "--no-trunc").stdout.split()), \
            "pre-existing containers were removed"
        assert "stale" in managed.values() and "active" in managed.values()

        # Once the formerly active run dies, it becomes stale and is recoverable.
        active_proc.send_signal(signal.SIGKILL)
        active_proc.wait(10)
        with Orchestrator(docker=live_docker, workspace_root=root) as recovery:
            assert recovery.cleanup_stale()["removed_containers"] == [active_cid]
            assert recovery.cleanup_stale()["removed_containers"] == []  # idempotent
        assert _running(unlabeled) and _running(project_only)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        for cid in decoys:
            docker_cli("rm", "-f", cid, check=False)
        for run in (locals().get("stale_run"), locals().get("active_run")):
            if run:
                for cid in project_containers(run):
                    docker_cli("rm", "-f", cid, check=False)


# Test 9 -------------------------------------------------------------------------------
def test_repeated_execution_no_leaks(live_docker, tmp_path):
    volumes_before = set(docker_cli("volume", "ls", "-q").stdout.split())
    with Orchestrator(docker=live_docker, workspace_root=tmp_path / "ws") as o:
        results = [o.run_agent(TaskConfig(type="echo", params={"message": f"m{i}"})) for i in range(20)]
        assert [r.status for r in results] == ["completed"] * 20
        assert [r.output["message"] for r in results] == [f"m{i}" for i in range(20)]
        assert len({r.container_id for r in results}) == 20
        assert project_containers(o.run_id) == []
        saved = [p for p in (tmp_path / "ws").iterdir() if p.name.startswith("agent-")]
        assert len(saved) == 20  # only the structured result of each task is preserved
        assert all([f.name for f in p.iterdir()] == ["result.json"] for p in saved)
    assert set(docker_cli("volume", "ls", "-q").stdout.split()) == volumes_before


# Failure-path cleanup (Definition of Done #6) ---------------------------------------------
def test_interrupted_run_many_cleans_up(live_docker, tmp_path):
    with Orchestrator(docker=live_docker, workspace_root=tmp_path) as o:
        cfgs = [TaskConfig(type="sleep", params={"duration": 60}, timeout_s=120) for _ in range(2)]
        ids = [o.create_agent(c) for c in cfgs]
        for i in ids:
            o.start_agent(i)
        assert len(project_containers(o.run_id)) == 2
        o.close()  # what the CLI does on SIGINT/SIGTERM
        assert project_containers(o.run_id) == []


# CLI ----------------------------------------------------------------------------------------
def run_cli(*args, **kw):
    return subprocess.run([sys.executable, "-m", "orchestrator", *args], cwd=ROOT, capture_output=True,
                          text=True, timeout=120, **kw)


def test_cli_run_demo_status_cleanup(live_docker, tmp_path):
    ws = ["--workspace-root", str(tmp_path)]
    p = run_cli("run", "--type", "echo", "--message", "Hello Agent", *ws)
    assert p.returncode == 0 and json.loads(p.stdout)["output"] == {"message": "Hello Agent"}

    p = run_cli("run", "--type", "sleep", "--duration", "60", "--timeout", "3", *ws)
    assert p.returncode == 1 and json.loads(p.stdout)["status"] == "timed_out"

    p = run_cli("demo", "--agents", "3", "--duration", "1.5", *ws)
    demo = json.loads(p.stdout)
    assert p.returncode == 0 and demo["max_observed_overlap"] == 3 and demo["leaked_containers"] == 0

    assert run_cli("status", *ws).returncode == 0
    assert run_cli("cleanup", "--stale", *ws).returncode == 0
    assert run_cli("cleanup", *ws).returncode != 0  # --stale is required: no accidental wipe


def test_cli_sigterm_removes_container(live_docker, tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-m", "orchestrator", "run", "--type", "sleep", "--duration", "60",
         "--timeout", "120", "--workspace-root", str(tmp_path)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 30
        while time.time() < deadline and not project_containers_for_workspace(tmp_path):
            time.sleep(0.2)
        assert project_containers_for_workspace(tmp_path), "container never appeared"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(30) == 130
        assert project_containers_for_workspace(tmp_path) == []
    finally:
        if proc.poll() is None:
            proc.kill()


def project_containers_for_workspace(tmp_path):
    runs = [p.stem for p in (tmp_path / ".runs").glob("*.lock")]
    return [cid for run in runs for cid in project_containers(run)]
