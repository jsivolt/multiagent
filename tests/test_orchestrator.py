"""Unit tests for orchestrator logic against an in-memory fake Docker (NOT runtime proof)."""
import threading
import time

import pytest

from fake_docker import FakeDocker
from orchestrator import ErrorKind, Orchestrator, TaskConfig
from orchestrator.docker_client import DockerError, build_create_args
from orchestrator.lifecycle import AgentState as S, InvalidTransition, RunLease
from orchestrator.manager import OrchestratorClosed
from orchestrator.models import (
    LABEL_PROJECT,
    LABEL_RUN_ID,
    ContainerSpec,
    ValidationError,
)


@pytest.fixture
def fake():
    return FakeDocker()


@pytest.fixture
def orch(fake, tmp_path):
    o = Orchestrator(docker=fake, workspace_root=tmp_path, max_concurrency=3, poll_interval_s=0.01)
    yield o
    o.close()


def cfg(type_="echo", **kw):
    params = kw.pop("params", {})
    return TaskConfig(type=type_, params=params, **kw)


# ---- hardening flags ---------------------------------------------------------------
def test_create_args_contain_all_isolation_settings():
    spec = ContainerSpec(name="n", image="img", labels={"a": "b"}, env={"K": "V"},
                         cpus=0.5, memory_mb=256, pids_limit=64)
    args = build_create_args(spec)

    def val(flag):
        return args[args.index(flag) + 1]

    assert val("--network") == "none"
    assert "--read-only" in args
    assert val("--user") == "10001:10001"
    assert val("--cap-drop") == "ALL"
    assert val("--security-opt") == "no-new-privileges=true"
    assert (val("--cpus"), val("--memory"), val("--memory-swap"), val("--pids-limit")) == ("0.5", "256m", "256m", "64")
    assert val("--tmpfs").startswith("/workspace:rw,noexec,nosuid,nodev,size=16m")
    for forbidden in ("--privileged", "--pid", "--ipc", "--volume", "-v", "--mount", "--device"):
        assert forbidden not in args
    assert not any("docker.sock" in a for a in args)


# ---- validation / ids ------------------------------------------------------------------
@pytest.mark.parametrize(
    "kw",
    [dict(type_="rm"), dict(timeout_s=0), dict(memory_mb=1), dict(pids_limit=1), dict(cpus=0),
     dict(task_id="../x"), dict(params={"x": object()})],
)
def test_invalid_config_rejected(orch, kw):
    with pytest.raises(ValidationError):
        orch.create_agent(cfg(**kw))


def test_agent_and_container_ids_unique_and_labeled(orch, fake):
    ids = [orch.create_agent(cfg()) for _ in range(20)]
    assert len(set(ids)) == 20
    cids = [orch.get_agent_status(i)["container_id"] for i in ids]
    assert len(set(cids)) == 20
    labels = fake.containers[cids[0]]["spec"].labels
    assert labels[LABEL_PROJECT] == "phase1" and labels[LABEL_RUN_ID] == orch.run_id


# ---- result classification --------------------------------------------------------------
def test_success_is_completed_and_container_removed(orch, fake):
    result = orch.run_agent(cfg(params={"message": "hi"}))
    assert (result.status, result.exit_code, result.error) == ("completed", 0, None)
    assert fake.containers == {}
    assert (orch.workspace_root / result.agent_id / "result.json").exists()


def test_app_failure_reports_agent_error(orch):
    r = orch.run_agent(cfg("fail"))
    assert r.status == "failed" and r.exit_code == 1
    assert r.error["kind"] == ErrorKind.APP_FAILURE and "intentional failure" in r.error["message"]


def test_exit_zero_without_valid_result_is_not_success(orch, fake):
    fake.behavior["echo"] = {"result": None}
    r = orch.run_agent(cfg())
    assert r.status == "failed" and r.error["kind"] == "app_failure"


def test_exit_zero_with_failed_result_is_not_success(orch, fake):
    fake.behavior["echo"] = {"result": {"status": "failed", "output": None, "error": {"message": "x"}}}
    assert orch.run_agent(cfg()).status == "failed"


def test_oom_is_app_failure(orch, fake):
    fake.behavior["echo"] = {"exit_code": 137, "oom": True, "result": None}
    r = orch.run_agent(cfg())
    assert r.status == "failed" and "OOM" in r.error["message"]


def test_runtime_start_error_is_startup_failure(orch, fake):
    fake.behavior["echo"] = {"exit_code": 127, "error": "OCI runtime create failed", "result": None}
    r = orch.run_agent(cfg())
    assert r.error["kind"] == ErrorKind.STARTUP_FAILURE


def test_docker_start_failure_is_startup_failure_and_cleans_up(orch, fake):
    fake.start_error = DockerError("boom")
    r = orch.run_agent(cfg())
    assert (r.status, r.error["kind"]) == ("failed", "startup_failure")
    assert fake.containers == {}


def test_create_failure_and_missing_image_classified(orch, fake):
    fake.create_error = DockerError("no space")
    assert orch.run_agent(cfg()).error["kind"] == "startup_failure"
    fake.create_error = DockerError("daemon", unavailable=True)
    assert orch.run_agent(cfg()).error["kind"] == "infrastructure"
    fake.create_error = None
    r = orch.run_agent(cfg(image="missing:latest"))
    assert r.error["kind"] == "infrastructure" and "not found" in r.error["message"]


def test_timeout_kills_container_and_reports_timed_out(orch, fake):
    fake.behavior["sleep"] = {"hang": True}
    t0 = time.monotonic()
    r = orch.run_agent(cfg("sleep", params={"duration": 60}, timeout_s=0.3))
    assert time.monotonic() - t0 < 3
    assert (r.status, r.error["kind"]) == ("timed_out", "timeout")
    assert fake.containers == {}


def test_cancel_running_agent(orch, fake):
    fake.behavior["sleep"] = {"hang": True}
    agent_id = orch.create_agent(cfg("sleep", params={"duration": 60}, timeout_s=30))
    orch.start_agent(agent_id)
    assert orch.stop_agent(agent_id) is S.CANCELLED
    assert orch.collect_agent_result(agent_id).status == "cancelled"
    orch.destroy_agent(agent_id)
    assert fake.containers == {}


def test_cancel_before_start_and_cannot_start_afterwards(orch):
    agent_id = orch.create_agent(cfg())
    orch.stop_agent(agent_id)
    assert orch.collect_agent_result(agent_id).error["kind"] == "cancelled"
    with pytest.raises(InvalidTransition):
        orch.start_agent(agent_id)


def test_docker_connection_loss_is_infrastructure_failure(orch, fake):
    fake.behavior["sleep"] = {"hang": True}
    agent_id = orch.create_agent(cfg("sleep", params={"duration": 5}))
    orch.start_agent(agent_id)

    def broken(_):
        raise DockerError("cannot connect", unavailable=True)

    fake.inspect_container = broken
    assert orch.collect_agent_result(agent_id).error["kind"] == "infrastructure"


# ---- cleanup idempotence ------------------------------------------------------------------
def test_destroy_is_idempotent_and_tolerates_missing_container(orch, fake):
    agent_id = orch.create_agent(cfg())
    orch.start_agent(agent_id)
    orch.collect_agent_result(agent_id)
    fake.containers.clear()  # someone already removed it
    orch.destroy_agent(agent_id)
    orch.destroy_agent(agent_id)
    assert orch.get_agent_status(agent_id)["state"] == "REMOVED"


def test_destroy_of_running_agent_cancels_first(orch, fake):
    fake.behavior["sleep"] = {"hang": True}
    agent_id = orch.create_agent(cfg("sleep", params={"duration": 60}))
    orch.start_agent(agent_id)
    orch.destroy_agent(agent_id)
    assert fake.containers == {}
    assert orch._get(agent_id).result.status == "cancelled"


def test_closed_orchestrator_refuses_new_agents(orch):
    orch.shutdown_agents()
    with pytest.raises(OrchestratorClosed):
        orch.create_agent(cfg())


# ---- concurrency ------------------------------------------------------------------------
def test_concurrency_limit_enforced_and_overlap_happens(fake, tmp_path):
    with Orchestrator(docker=fake, workspace_root=tmp_path, max_concurrency=2, poll_interval_s=0.01) as o:
        results = o.run_many([cfg("sleep", params={"duration": 0.3}) for _ in range(6)])
    assert [r.status for r in results] == ["completed"] * 6
    assert fake.peak_running == 2  # limit reached, never exceeded
    assert len({r.container_id for r in results}) == 6


def test_failure_does_not_affect_other_agents(orch, fake):
    cfgs = [cfg("sleep", params={"duration": 0.2}), cfg("fail"), cfg("sleep", params={"duration": 0.2})]
    results = orch.run_many(cfgs)
    assert [r.status for r in results] == ["completed", "failed", "completed"]
    assert fake.containers == {}


def test_waiting_for_slot_can_be_cancelled(fake, tmp_path):
    fake.behavior["sleep"] = {"hang": True}
    with Orchestrator(docker=fake, workspace_root=tmp_path, max_concurrency=1, poll_interval_s=0.01) as o:
        first = o.create_agent(cfg("sleep", params={"duration": 60}))
        o.start_agent(first)
        second = o.create_agent(cfg("sleep", params={"duration": 60}))
        t = threading.Thread(target=o.start_agent, args=(second,))
        t.start()
        time.sleep(0.3)
        assert o._get(second).state is S.CREATED  # blocked on the limit
        o.stop_agent(second)
        t.join(5)
        assert not t.is_alive() and o._get(second).result.status == "cancelled"


# ---- stale cleanup selection ---------------------------------------------------------------
def test_cleanup_stale_only_removes_owned_stale_containers(orch, fake):
    own = orch.create_agent(cfg())
    own_cid = orch.get_agent_status(own)["container_id"]

    stale = fake.add_foreign({LABEL_PROJECT: "phase1", LABEL_RUN_ID: "deadrun"}, "stale")
    active_lease = RunLease.acquire(orch.state_dir, "liverun")
    active = fake.add_foreign({LABEL_PROJECT: "phase1", LABEL_RUN_ID: "liverun"}, "active")
    unowned = fake.add_foreign({LABEL_PROJECT: "phase1"}, "project-label-only")
    other_project = fake.add_foreign({"app": "nextcloud"}, "unrelated")

    report = orch.cleanup_stale()
    assert report["removed_containers"] == [stale]
    assert report["skipped_active_runs"] == ["liverun"]
    assert unowned in report["skipped_unowned_containers"]
    assert set(fake.containers) == {own_cid, active, unowned, other_project}
    assert orch.cleanup_stale()["removed_containers"] == []  # idempotent

    active_lease.release()  # its owner "died": now the run is stale and gets cleaned
    assert orch.cleanup_stale()["removed_containers"] == [active]
