"""Live Docker isolation tests (Tests 6 and 7): configuration is inspected AND enforcement is probed
from inside running containers."""
import json
import textwrap

import pytest

from conftest import docker_cli
from orchestrator import TaskConfig

pytestmark = pytest.mark.live


@pytest.fixture
def agent(orch):
    """Factory for a running long-lived agent container; returns (agent_id, container_id)."""

    def start():
        agent_id = orch.create_agent(TaskConfig(type="sleep", params={"duration": 120}, timeout_s=300))
        orch.start_agent(agent_id)
        return agent_id, orch.get_agent_status(agent_id)["container_id"]

    return start


def sh(cid, script, check=False):
    return docker_cli("exec", cid, "sh", "-c", script, check=check)


def py(cid, code):
    return docker_cli("exec", cid, "python", "-c", textwrap.dedent(code), check=False)


def inspect(cid):
    return json.loads(docker_cli("inspect", cid).stdout)[0]


# ---- Test 6: resource limits --------------------------------------------------------------
def test_limits_configured(agent):
    _, cid = agent()
    hc = inspect(cid)["HostConfig"]
    assert hc["NanoCpus"] == 500_000_000
    assert hc["Memory"] == hc["MemorySwap"] == 256 * 1024**2
    assert hc["PidsLimit"] == 64


def test_limits_visible_in_cgroup(agent):
    _, cid = agent()
    assert sh(cid, "cat /sys/fs/cgroup/pids.max").stdout.strip() == "64"
    assert sh(cid, "cat /sys/fs/cgroup/memory.max").stdout.strip() == str(256 * 1024**2)
    assert sh(cid, "cat /sys/fs/cgroup/memory.swap.max").stdout.strip() == "0"
    assert sh(cid, "cat /sys/fs/cgroup/cpu.max").stdout.split() == ["50000", "100000"]


def test_pid_limit_enforced(agent):
    _, cid = agent()
    p = py(cid, """
        import os, time
        pids = []
        try:
            for _ in range(200):
                pid = os.fork()
                if pid == 0:
                    time.sleep(5); os._exit(0)
                pids.append(pid)
        except OSError as exc:
            print("blocked", len(pids), exc.errno)
        finally:
            for pid in pids:
                os.kill(pid, 9)
    """)
    assert p.stdout.startswith("blocked"), p.stdout + p.stderr
    forked = int(p.stdout.split()[1])
    assert 0 < forked < 64


def test_memory_limit_enforced(agent):
    _, cid = agent()
    p = py(cid, "b = bytearray(600 * 1024 * 1024); b[::4096] = b'x' * len(b[::4096]); print('allocated')")
    assert p.returncode == 137 and "allocated" not in p.stdout  # OOM-killed, not allowed 600MB
    assert inspect(cid)["State"]["Running"], "container itself must survive its child being OOM-killed"


def test_cpu_limit_enforced(agent):
    _, cid = agent()
    p = py(cid, """
        import time
        def usage():
            return int([l for l in open('/sys/fs/cgroup/cpu.stat') if l.startswith('usage_usec')][0].split()[1])
        def throttled():
            return int([l for l in open('/sys/fs/cgroup/cpu.stat') if l.startswith('nr_throttled')][0].split()[1])
        u0, t0, w0 = usage(), throttled(), time.monotonic()
        while time.monotonic() - w0 < 3:
            pass
        wall = time.monotonic() - w0
        print((usage() - u0) / 1e6 / wall, throttled() - t0)
    """)
    cpu_share, throttled = (float(x) for x in p.stdout.split())
    assert cpu_share < 0.65, f"busy loop used {cpu_share:.2f} CPUs, limit is 0.5"
    assert throttled > 0


# ---- Test 7: security isolation -----------------------------------------------------------------
def test_security_configuration(agent):
    _, cid = agent()
    doc = inspect(cid)
    hc, cfg = doc["HostConfig"], doc["Config"]
    assert hc["Privileged"] is False
    assert hc["NetworkMode"] == "none"
    assert hc["ReadonlyRootfs"] is True
    assert hc["CapDrop"] == ["ALL"] and not hc["CapAdd"]
    assert "no-new-privileges=true" in hc["SecurityOpt"]
    assert not any("seccomp" in o and "unconfined" in o for o in hc["SecurityOpt"])
    assert hc["PidMode"] == "" and hc["IpcMode"] in ("private", "") and hc["UsernsMode"] == ""
    assert not hc["Binds"] and not hc["Devices"]
    assert doc["Mounts"] == [], "no volume or bind mounts at all"
    assert list(hc["Tmpfs"]) == ["/workspace"]
    assert cfg["User"] == "10001:10001"
    env_keys = {e.split("=")[0] for e in cfg["Env"]}
    assert env_keys <= {"PATH", "TASK_JSON", "AGENT_ID", "WORKSPACE", "PYTHONDONTWRITEBYTECODE",
                        "PYTHONUNBUFFERED", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256"}, env_keys


def test_runtime_identity_and_kernel_restrictions(agent):
    _, cid = agent()
    assert sh(cid, "id -u").stdout.strip() == "10001"
    status = sh(cid, "cat /proc/self/status").stdout
    fields = dict(line.split(":\t", 1) for line in status.splitlines() if ":\t" in line)
    assert int(fields["CapBnd"], 16) == 0 and int(fields["CapEff"], 16) == 0, "capabilities not dropped"
    assert fields["NoNewPrivs"].strip() == "1"
    assert fields["Seccomp"].strip() == "2", "default seccomp filter not active"


def test_no_docker_socket_or_host_home(agent, tmp_path):
    _, cid = agent()
    for path in ("/var/run/docker.sock", "/run/docker.sock", "/home/celltester", "/host", str(tmp_path)):
        assert sh(cid, f"test -e {path}").returncode != 0, f"{path} visible inside container"
    mounts = sh(cid, "cat /proc/mounts").stdout
    assert "docker.sock" not in mounts and "celltester" not in mounts and str(tmp_path) not in mounts


def test_root_filesystem_read_only_and_only_workspace_writable(agent):
    _, cid = agent()
    for path in ("/etc/pwn", "/app/pwn", "/usr/pwn", "/tmp/pwn", "/pwn", "/home/pwn"):
        assert sh(cid, f"touch {path}").returncode != 0, f"{path} is writable"
    assert sh(cid, "echo ok > /workspace/probe && cat /workspace/probe").stdout.strip() == "ok"
    assert "size=16384k" in sh(cid, "grep ' /workspace ' /proc/mounts").stdout
    assert sh(cid, "df -k /workspace | tail -1").stdout.split()[1] == "16384"


def test_workspace_cannot_execute_binaries(agent):
    _, cid = agent()
    r = sh(cid, "cp /bin/true /workspace/t && chmod +x /workspace/t && /workspace/t")
    assert r.returncode != 0  # tmpfs mounted noexec


def test_network_disabled(agent):
    _, cid = agent()
    ifaces = sh(cid, "ls /sys/class/net").stdout.split()
    assert ifaces == ["lo"], ifaces
    p = py(cid, """
        import socket
        try:
            socket.create_connection(("1.1.1.1", 53), timeout=2); print("connected")
        except OSError as exc:
            print("blocked", exc.errno)
    """)
    assert p.stdout.startswith("blocked"), p.stdout


def test_cannot_escalate_to_root(agent):
    _, cid = agent()
    assert sh(cid, "su -c id root 2>&1; sudo -n id 2>&1").stdout.count("uid=0") == 0
    p = py(cid, "import os; os.setuid(0)")
    assert p.returncode != 0


def test_agents_cannot_see_each_others_workspace(agent, orch):
    (a_id, a), (b_id, b) = agent(), agent()
    assert a != b
    assert sh(a, "echo secret-A > /workspace/secret").returncode == 0
    assert sh(b, "ls -A /workspace").stdout.strip() == "", "B sees A's workspace content"
    assert sh(b, "cat /workspace/secret").returncode != 0
    # No path to A's files from B: separate mount namespaces and no shared mounts.
    assert sh(b, f"test -e /proc/{1}/root/workspace/secret").returncode != 0
    assert sh(a, "cat /workspace/secret").stdout.strip() == "secret-A"
    assert not any(a_id in line or orch.workspace_root.as_posix() in line
                   for line in sh(b, "cat /proc/mounts").stdout.splitlines())
    assert inspect(a)["Mounts"] == inspect(b)["Mounts"] == []
