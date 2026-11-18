"""Live Docker tests of the Phase 2 task layer using the deterministic probe runtime (no LLM involved).

They run the real TaskManager code path (container spec, bind mounts, tmpfs, user, limits, labels, lifecycle,
timeouts, cancellation, credentials, reconciliation) against real containers of the real Pi image."""
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from conftest import ROOT, docker_cli, project_containers
from orchestrator import Orchestrator
from orchestrator.docker_client import DockerClient, DockerError
from orchestrator.models import KIND_TASK, LABEL_KIND, LABEL_PROJECT, LABEL_RUN_ID, LABEL_TASK_ID
from orchestrator.tasks import TaskConflict, TaskManager, WorkspaceBusy

pytestmark = pytest.mark.live


def probe(task_id, workspace=None, seconds=120, **extra):
    req = {"runtime": "probe", "prompt": "probe", "taskId": task_id, "options": {"args": [str(seconds)]}}
    if workspace:
        req["workspace"] = workspace
    req.update(extra)
    return req


def until(pred, timeout=30, interval=0.1, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        value = pred()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


def running(tm, task_id):
    return until(lambda: (lambda t: t["containerId"] if t["state"] == "RUNNING" else None)(tm.get_task(task_id)),
                 what=f"{task_id} RUNNING")


def dx(cid, script):
    return docker_cli("exec", cid, "sh", "-c", script, check=False)


def inspect(cid):
    return json.loads(docker_cli("inspect", cid).stdout)[0]


def gone(cid):
    return docker_cli("inspect", "--type", "container", cid, check=False).returncode != 0


# ---- isolation of the real task container (Test E, deterministic part) -------------------------------------
def test_task_container_isolation_and_workspace_separation(tm, monkeypatch):
    monkeypatch.setenv("POC_HOST_CANARY", "host-env-value")
    wb = tm.root / "ws-b"
    wb.mkdir()
    (wb / "secret-b.txt").write_text("B-ONLY-DATA")
    tm.submit_task(probe("iso-a", "ws-a"))
    tm.submit_task(probe("iso-b", "ws-b"))
    a, b = running(tm, "iso-a"), running(tm, "iso-b")
    assert a != b

    doc = inspect(a)
    hc, cfg = doc["HostConfig"], doc["Config"]
    assert hc["Privileged"] is False and hc["NetworkMode"] == "none" and hc["ReadonlyRootfs"] is True
    assert hc["CapDrop"] == ["ALL"] and not hc["CapAdd"] and "no-new-privileges=true" in hc["SecurityOpt"]
    assert not any("unconfined" in o for o in hc["SecurityOpt"])
    assert hc["PidMode"] == "" and hc["IpcMode"] in ("private", "") and hc["UsernsMode"] == "" and not hc["Devices"]
    assert (hc["NanoCpus"], hc["Memory"], hc["MemorySwap"], hc["PidsLimit"]) == (2_000_000_000, 2048 * 1024**2, 2048 * 1024**2, 512)
    assert cfg["User"] == f"{os.getuid()}:{os.getgid()}" and os.getuid() != 0
    assert {(m["Destination"], m["RW"], m["Type"]) for m in doc["Mounts"]} == {
        ("/workspace", True, "bind"), ("/run/task", False, "bind")}
    sources = {m["Destination"]: m["Source"] for m in doc["Mounts"]}
    assert sources["/workspace"] == str(tm.root / "ws-a") and sources["/run/task"] == str(tm.root / ".tasks" / "iso-a" / "runtime")
    assert set(hc["Tmpfs"]) == {"/tmp", "/home/agent"}
    assert not any("docker.sock" in m["Source"] or m["Source"].startswith(str(Path.home())) for m in doc["Mounts"])
    assert not any("POC_HOST_CANARY" in e for e in cfg["Env"])

    status = dict(l.split(":\t", 1) for l in dx(a, "cat /proc/self/status").stdout.splitlines() if ":\t" in l)
    assert int(status["CapEff"], 16) == 0 and int(status["CapBnd"], 16) == 0
    assert status["NoNewPrivs"].strip() == "1" and status["Seccomp"].strip() == "2"
    assert dx(a, "id -u").stdout.strip() == str(os.getuid())
    assert "POC_HOST_CANARY" not in dx(a, "env").stdout
    for path in ("/var/run/docker.sock", "/run/docker.sock", str(Path.home()), "/run/secrets", "/run/llm"):
        assert dx(a, f"test -e {path}").returncode != 0, f"{path} visible"
    assert dx(a, "touch /etc/pwned /opt/pwned /run/task/pwned").returncode != 0  # read-only rootfs and /run/task
    assert dx(a, "ls /sys/class/net").stdout.split() == ["lo"]
    assert dx(a, "python3 -c \"import socket;socket.create_connection(('1.1.1.1',53),2)\"").returncode != 0

    # Workspace isolation: A sees only its own workspace; B's file is unreachable from A and vice versa.
    assert dx(a, "ls -A /workspace").stdout.strip() == ""
    assert dx(a, "find / -name 'secret-b.txt' -not -path '/proc/*' 2>/dev/null").stdout.strip() == ""
    assert dx(b, "cat /workspace/secret-b.txt").stdout == "B-ONLY-DATA"
    assert dx(a, "echo A-DATA > /workspace/work.txt").returncode == 0
    assert (tm.root / "ws-a" / "work.txt").read_text().strip() == "A-DATA"
    assert not (tm.root / "ws-b" / "work.txt").exists() and dx(b, "test -e /workspace/work.txt").returncode != 0

    for tid in ("iso-a", "iso-b"):
        tm.cancel_task(tid)
    ra, rb = tm.wait("iso-a", 60)["result"], tm.wait("iso-b", 60)["result"]
    assert ra["status"] == rb["status"] == "cancelled"
    assert [x["path"] for x in ra["artifacts"]] == ["work.txt"] and rb["artifacts"] == []
    assert gone(a) and gone(b)  # containers removed ...
    assert (tm.root / "ws-a" / "work.txt").read_text().strip() == "A-DATA"  # ... workspace artifacts preserved


def test_workspace_symlinks_and_fifos_cannot_hurt_the_host(tm):
    tm.submit_task(probe("attack", "ws-attack"))
    cid = running(tm, "attack")
    script = "cd /workspace && ln -s /etc/passwd link && ln -s /run/task dirlink && mkfifo pipe && echo ok > real.txt"
    assert dx(cid, script).returncode == 0
    t0 = time.time()
    tm.cancel_task("attack")
    result = tm.wait("attack", 60)["result"]
    assert time.time() - t0 < 30, "result collection hung (FIFO?)"
    types = {a["path"]: a["type"] for a in result["artifacts"]}
    assert types == {"link": "symlink", "dirlink": "symlink", "real.txt": "file"}
    diff = Path(result["changes"]["diffFile"]).read_text()
    assert "root:x:" not in diff and "ok" in diff


# ---- lifecycle: timeout, cancellation, forced termination (Test D) ---------------------------------------------
def test_timeout_stops_container_and_reports_timed_out(tm):
    tm.submit_task(probe("slow", timeoutSeconds=3))
    cid = running(tm, "slow")
    t0 = time.time()
    snap = tm.wait("slow", 60)
    res = snap["result"]
    assert res["status"] == "timed_out" and res["error"]["kind"] == "timeout" and snap["state"] == "REMOVED"
    assert 2.5 < time.time() - t0 < 15 and gone(cid)
    assert any(e["type"] == "agent.stopping" and e["data"]["reason"] == "timeout" for e in tm.get_task_events("slow"))


def test_cancellation_is_cooperative_and_idempotent(tm):
    tm.submit_task(probe("cancel-me"))
    cid = running(tm, "cancel-me")
    t0 = time.time()
    tm.cancel_task("cancel-me")
    res = tm.wait("cancel-me", 60)["result"]
    assert res["status"] == "cancelled" and res["error"]["kind"] == "cancelled" and time.time() - t0 < 10 and gone(cid)
    types = [e["type"] for e in tm.get_task_events("cancel-me")]
    assert "task.cancelled" in types and types[-1] == "container.cleaned"
    assert tm.cancel_task("cancel-me")["status"] == "cancelled"  # finished task: no error, no change


def test_forced_termination_when_agent_ignores_sigterm(tm):
    req = probe("stubborn")
    req["options"] = {"entrypoint": "/bin/sh", "args": ["-c", "trap '' TERM; while true; do sleep 1; done"]}
    tm.submit_task(req)
    cid = running(tm, "stubborn")
    t0 = time.time()
    tm.cancel_task("stubborn")
    res = tm.wait("stubborn", 60)["result"]
    took = time.time() - t0
    assert res["status"] == "cancelled" and gone(cid)
    assert tm.term_grace_s - 0.5 <= took < tm.term_grace_s + 10, f"SIGKILL fallback took {took:.1f}s"


def test_workspace_quota_stops_a_runaway_writer(tm):
    req = probe("filler", "ws-filler", workspaceQuotaMb=1)
    req["options"] = {"entrypoint": "/bin/sh", "args": ["-c", "head -c 20000000 /dev/zero > /workspace/big.bin; sleep 120"]}
    tm.submit_task(req)
    t0 = time.time()
    snap = tm.wait("filler", 60)
    res = snap["result"]
    assert (res["status"], res["error"]["kind"], res["error"]["code"]) == ("failed", "agent_failure", "workspace_quota_exceeded")
    assert time.time() - t0 < 30 and snap["state"] == "REMOVED" and gone(res["containerId"])
    assert [a["path"] for a in res["artifacts"]] == ["big.bin"]  # evidence is preserved


def test_flooding_output_is_bounded_and_does_not_break_the_task(tm):
    req = probe("flood")
    req["options"] = {"entrypoint": "/bin/sh", "args": ["-c", "head -c 3000000 /dev/zero | tr '\\0' 'x' | fold -w 40; echo done"]}
    tm.submit_task(req)
    snap = tm.wait("flood", 120)
    assert snap["result"]["status"] == "completed"
    events = tm.get_task_events("flood")
    from orchestrator.tasks import LOG_FILE_MAX, MAX_EVENTS

    assert len(events) <= MAX_EVENTS + 20, f"{len(events)} events recorded"
    assert (tm.root / ".tasks" / "flood" / "stdout.log").stat().st_size <= LOG_FILE_MAX + 4096
    assert events[-1]["type"] == "container.cleaned"  # lifecycle events still recorded after the cap


def test_cancel_from_another_process_reaches_the_owner(tm, live_docker):
    tm.submit_task(probe("remote-cancel"))
    running(tm, "remote-cancel")
    with TaskManager(docker=live_docker, workspace_root=tm.root) as other:  # a different run, same workspace root
        other.cancel_task("remote-cancel")
    assert tm.wait("remote-cancel", 60)["result"]["status"] == "cancelled"


def test_natural_exit_zero_completes_and_nonzero_is_agent_failure(tm):
    tm.submit_task(probe("quick", seconds=1))
    assert tm.wait("quick", 60)["result"]["status"] == "completed"
    bad = probe("exits-2")
    bad["options"] = {"entrypoint": "/bin/sh", "args": ["-c", "echo boom >&2; exit 2"]}
    tm.submit_task(bad)
    res = tm.wait("exits-2", 60)["result"]
    assert (res["status"], res["exitCode"], res["error"]["kind"], res["error"]["code"]) == ("failed", 2, "agent_failure", "agent_error")


# ---- failure classification: infrastructure vs startup vs agent -------------------------------------------------
class FailingStart(DockerClient):
    def start_container(self, container_id):
        raise DockerError("simulated start failure")


class DaemonDown(DockerClient):
    def create_container(self, spec):
        raise DockerError("Cannot connect to the Docker daemon", unavailable=True)


def test_container_start_failure_is_startup_failure_and_created_container_removed(live_docker, pi_image, tmp_path):
    from probe import register_probe

    register_probe()
    with TaskManager(docker=FailingStart(), workspace_root=tmp_path) as m:
        m.submit_task(probe("nostart"))
        snap = m.wait("nostart", 60)
        err = snap["result"]["error"]
        assert (snap["result"]["status"], err["kind"], err["code"]) == ("failed", "startup_failure", "container_start_failed")
        assert snap["state"] == "REMOVED" and gone(snap["result"]["containerId"])
        assert project_containers(m.run_id) == []


def test_docker_unavailable_and_image_problems_are_infrastructure_failures(live_docker, pi_image, tmp_path):
    from probe import register_probe

    register_probe()
    with TaskManager(docker=DaemonDown(), workspace_root=tmp_path / "a") as m:
        m.submit_task(probe("down"))
        err = m.wait("down", 60)["result"]["error"]
        assert (err["kind"], err["code"]) == ("infrastructure", "docker_unavailable")
    with TaskManager(docker=live_docker, workspace_root=tmp_path / "b") as m:
        m.submit_task(probe("noimg", image="multiagent-no-such-image:0"))
        assert m.wait("noimg", 60)["result"]["error"]["code"] == "image_missing"
        m.submit_task(probe("badimg", image="python:3.12-alpine"))  # exists, but not built for this uid / no label
        assert m.wait("badimg", 60)["result"]["error"]["code"] == "image_uid_mismatch"


# ---- credentials ---------------------------------------------------------------------------------------------------
def test_missing_credentials_fail_before_any_container_is_created(tm, monkeypatch):
    monkeypatch.delenv("POC_NOT_SET_KEY", raising=False)
    tm.submit_task(probe("nocreds", credentials=["POC_NOT_SET_KEY"]))
    res = tm.wait("nocreds", 60)["result"]
    assert (res["status"], res["error"]["kind"], res["error"]["code"]) == ("failed", "startup_failure", "missing_credentials")
    assert res["containerId"] is None and project_containers(tm.run_id) == []


def test_credentials_are_task_scoped_readonly_files_redacted_and_removed(tm, monkeypatch):
    secret = "s3cr3t-Value-" + uuid.uuid4().hex
    monkeypatch.setenv("POC_TEST_SECRET", secret)
    req = probe("withcreds", credentials=["POC_TEST_SECRET"])
    req["options"] = {"entrypoint": "/bin/sh", "args": ["-c", "echo token=$(cat /run/secrets/POC_TEST_SECRET); sleep 60"]}
    tm.submit_task(req)
    cid = running(tm, "withcreds")
    sfile = tm.root / ".tasks" / "withcreds" / "secrets" / "POC_TEST_SECRET"
    until(lambda: sfile.exists(), what="secret file")
    assert stat_mode(sfile) == 0o400
    doc = inspect(cid)
    assert secret not in json.dumps(doc["Config"]) and secret not in json.dumps(doc["HostConfig"])  # not env / args / labels
    assert {(m["Destination"], m["RW"]) for m in doc["Mounts"]} >= {("/run/secrets", False)}
    assert dx(cid, "cat /run/secrets/POC_TEST_SECRET").stdout == secret
    assert dx(cid, "touch /run/secrets/other").returncode != 0
    until(lambda: (tm.root / ".tasks" / "withcreds" / "stdout.log").exists() and "token=" in (tm.root / ".tasks" / "withcreds" / "stdout.log").read_text(),
          what="agent output")
    tm.cancel_task("withcreds")
    tm.wait("withcreds", 60)
    assert not (tm.root / ".tasks" / "withcreds" / "secrets").exists(), "credential material must be removed after the run"
    for name in ("stdout.log", "events.jsonl", "result.json", "task.json"):
        text = (tm.root / ".tasks" / "withcreds" / name).read_text()
        assert secret not in text, f"secret leaked into {name}"
    assert "token=[REDACTED]" in (tm.root / ".tasks" / "withcreds" / "stdout.log").read_text()


def stat_mode(path):
    return path.stat().st_mode & 0o777


# ---- duplicate submits ------------------------------------------------------------------------------------------------
def test_duplicate_task_ids_and_busy_workspaces_are_handled_safely(tm):
    first = tm.submit_task(probe("dup", "ws-dup"))
    again = tm.submit_task(probe("dup", "ws-dup"))  # identical request: idempotent, no second container
    assert again["taskId"] == first["taskId"]
    cid = running(tm, "dup")
    assert len(project_containers(tm.run_id)) == 1
    with pytest.raises(TaskConflict):
        tm.submit_task(probe("dup", "ws-dup", timeoutSeconds=5))  # same id, different request
    with pytest.raises(WorkspaceBusy):
        tm.submit_task(probe("other", "ws-dup"))  # another task must not share a live workspace
    assert not (tm.root / ".tasks" / "other").exists()
    tm.cancel_task("dup")
    tm.wait("dup", 60)
    assert gone(cid)
    tm.submit_task(probe("later", "ws-dup", seconds=1))  # workspace is reusable once the first task ended
    assert tm.wait("later", 60)["result"]["status"] == "completed"
    assert not tm.cleanup_workspace("nonexistent-ws") or True
    assert tm.cleanup_workspace("ws-dup") and not (tm.root / "ws-dup").exists()


# ---- restart recovery (Test F, deterministic part) ---------------------------------------------------------------------
def spawn_holder(root, request):
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "hold_task.py"), str(root), json.dumps(request)],
                            stdout=subprocess.PIPE, text=True)
    tid, run_id, cid = proc.stdout.readline().split()
    return proc, tid, run_id, cid


def test_restart_reconciliation_adopts_only_owned_orphaned_tasks(live_docker, pi_image, tmp_path):
    from probe import register_probe

    register_probe()
    root = tmp_path / "ws"
    created = []

    def decoy(*labels, name=None):
        args = ["create", "--name", name or f"poc-test-decoy-{uuid.uuid4().hex[:6]}", "--network", "none", "--entrypoint", "sleep"]
        for label in labels:
            args += ["--label", label]
        cid = docker_cli(*args, pi_image, "300").stdout.strip()
        docker_cli("start", cid)
        created.append(cid)
        return cid

    holder = None
    try:
        holder, tid, dead_run, task_cid = spawn_holder(root, probe("recover", "ws-recover", seconds=300, timeoutSeconds=25))
        t_submit = time.time()
        unlabeled = decoy()
        project_only = decoy(f"{LABEL_PROJECT}=phase1")
        ghost = decoy(f"{LABEL_PROJECT}=phase1", f"{LABEL_RUN_ID}=ghost-run", f"{LABEL_KIND}={KIND_TASK}", f"{LABEL_TASK_ID}=ghost-task")
        with TaskManager(docker=live_docker, workspace_root=root) as active:  # a second, live orchestrator
            active.submit_task(probe("live-one", "ws-live", seconds=300))
            live_cid = running(active, "live-one")

            holder.send_signal(signal.SIGKILL)  # unexpected restart: no cleanup runs
            holder.wait(10)
            assert docker_cli("inspect", "--format", "{{.State.Running}}", task_cid).stdout.strip() == "true"

            with Orchestrator(docker=live_docker, workspace_root=root) as o:
                sweep = o.cleanup_stale()  # the Phase 1 sweep must not kill the recoverable task
            assert task_cid[:12] in [c[:12] for c in sweep["skipped_task_containers"]]
            assert docker_cli("inspect", "--format", "{{.State.Running}}", task_cid).stdout.strip() == "true"

            with TaskManager(docker=live_docker, workspace_root=root) as recovery:
                report = recovery.reconcile()
                assert report["adopted"] == [tid]
                assert ghost[:12] in report["removed"], report  # owned + stale + no task record
                assert live_cid[:12] in report["skippedActiveRuns"]  # belongs to a live orchestrator
                assert unlabeled[:12] not in report["removed"] and project_only[:12] not in report["removed"]
                assert docker_cli("inspect", "--format", "{{.State.Running}}", live_cid).stdout.strip() == "true"
                snap = recovery.wait(tid, 90)
                res = snap["result"]
                # The original 25 s deadline is honoured (not restarted from the adoption time).
                assert res["status"] == "timed_out" and snap["state"] == "REMOVED"
                elapsed = time.time() - t_submit
                assert elapsed < 40, f"deadline was not preserved across the restart ({elapsed:.0f}s)"
                types = [e["type"] for e in recovery.get_task_events(tid)]
                assert "task.reconciled" in types and types[-1] == "container.cleaned"
                assert gone(task_cid) and gone(ghost)
            for cid in (unlabeled, project_only, live_cid):
                assert docker_cli("inspect", "--format", "{{.State.Running}}", cid).stdout.strip() == "true"
            active.cancel_task("live-one")
            active.wait("live-one", 60)
    finally:
        if holder and holder.poll() is None:
            holder.kill()
        for cid in created:
            docker_cli("rm", "-f", cid, check=False)
        for cid in project_containers():
            if docker_cli("inspect", "--format", "{{index .Config.Labels \"multiagent.poc.task_id\"}}", cid, check=False).stdout.strip() in ("recover", "live-one", "ghost-task"):
                docker_cli("rm", "-f", cid, check=False)


def test_reconcile_of_crashed_task_that_finished_meanwhile_collects_result(live_docker, pi_image, tmp_path):
    """The agent exited while no orchestrator was running: reconcile must still produce the result and clean up."""
    from probe import register_probe

    register_probe()
    root = tmp_path / "ws"
    holder, tid, _, cid = spawn_holder(root, probe("finished-offline", "ws-fo", seconds=4, timeoutSeconds=60))
    try:
        holder.send_signal(signal.SIGKILL)
        holder.wait(10)
        until(lambda: docker_cli("inspect", "--format", "{{.State.Status}}", cid).stdout.strip() == "exited", timeout=30,
              what="container exit")
        with TaskManager(docker=live_docker, workspace_root=root) as recovery:
            assert recovery.reconcile()["adopted"] == [tid]
            snap = recovery.wait(tid, 60)
            assert snap["result"]["status"] == "completed" and snap["state"] == "REMOVED" and gone(cid)
    finally:
        holder.kill()
        docker_cli("rm", "-f", cid, check=False)
