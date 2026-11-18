import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from orchestrator import DockerClient, Orchestrator  # noqa: E402
from orchestrator.models import DEFAULT_IMAGE, LABEL_PROJECT, LABEL_PROJECT_VALUE  # noqa: E402


def docker_cli(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=120)


def project_containers(run_id=None):
    """IDs of all containers (any state) carrying the project label, optionally for one run."""
    flt = f"label={LABEL_PROJECT}={LABEL_PROJECT_VALUE}"
    args = ["ps", "-aq", "--no-trunc", "--filter", flt]
    if run_id:
        args += ["--filter", f"label=multiagent.poc.run_id={run_id}"]
    return docker_cli(*args).stdout.split()


@pytest.fixture(scope="session")
def live_docker():
    """Real DockerClient with the agent image built; skips the live tests if Docker is unusable."""
    client = DockerClient()
    try:
        client._run(["info", "--format", "{{.ServerVersion}}"])
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Docker daemon unavailable: {exc}")
    client.build_image(DEFAULT_IMAGE, str(ROOT / "agent"))
    return client


@pytest.fixture
def orch(live_docker, tmp_path):
    """Orchestrator on the real daemon; always removes whatever it created."""
    o = Orchestrator(docker=live_docker, workspace_root=tmp_path / "ws", max_concurrency=4)
    yield o
    o.close()
    assert project_containers(o.run_id) == [], "test leaked containers"


# ---- Phase 2 fixtures --------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def pi_image(live_docker):
    """Build (or reuse the cached) real Pi agent image for the current uid."""
    import os

    from orchestrator.runtimes.pi import DEFAULT_IMAGE as PI_IMAGE

    live_docker.build_image(PI_IMAGE, str(ROOT / "agent" / "pi"),
                            {"AGENT_UID": os.getuid(), "AGENT_GID": os.getgid()})
    return PI_IMAGE


@pytest.fixture
def tm(live_docker, pi_image, tmp_path):
    """TaskManager on the real daemon with the test-only probe runtime registered; asserts nothing leaks."""
    from probe import register_probe
    from orchestrator.tasks import TaskManager

    register_probe()
    manager = TaskManager(docker=live_docker, workspace_root=tmp_path / "ws", max_concurrency=3)
    yield manager
    manager.close()
    assert project_containers(manager.run_id) == [], "test leaked task containers"
