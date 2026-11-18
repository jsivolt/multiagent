"""Phase 2 unit tests: no Docker, no LLM. (Live behaviour: test_tasks_probe.py and test_e2e_pi.py.)"""
import http.client
import json
import os
import socket
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fake_docker import FakeDocker
from orchestrator import Orchestrator
from orchestrator import workspace as ws
from orchestrator.docker_client import DockerError, build_create_args
from orchestrator.lifecycle import AgentState as S, StateMachine
from orchestrator.llm_relay import LLMRelay
from orchestrator.models import KIND_TASK, LABEL_KIND, LABEL_PROJECT, LABEL_RUN_ID, ContainerSpec, ValidationError
from orchestrator.runtimes import get_runtime
from orchestrator.runtimes.pi import PiParser, PiRuntime
from orchestrator.tasks import Redactor, TaskRequest


# ---- request parsing / validation ------------------------------------------------------------
def test_request_accepts_flat_camel_and_nested_forms():
    flat = TaskRequest.from_dict({"runtime": "pi", "prompt": "p", "workspace": "task-001", "timeoutSeconds": 900})
    nested = TaskRequest.from_dict({
        "agent": {"runtime": "pi", "model": "m1", "timeoutSeconds": 900},
        "container": {"memory": "4g", "cpus": 2, "network": "none"},
        "prompt": "p", "workspace": "task-001"})
    assert flat.timeout_s == 900 and flat.workspace == "task-001"
    assert (nested.model, nested.memory_mb, nested.cpus, nested.network) == ("m1", 4096, 2.0, "none")


@pytest.mark.parametrize("bad", [
    {"prompt": ""}, {"prompt": "x", "workspace": "../etc"}, {"prompt": "x", "workspace": "/abs/path"},
    {"prompt": "x", "workspace": ".tasks"}, {"prompt": "x", "taskId": "a/b"}, {"prompt": "x", "model": "--evil"},
    {"prompt": "x", "provider": "a b"}, {"prompt": "x", "container": {"network": "restricted"}},
    {"prompt": "x", "container": {"network": "host"}}, {"prompt": "x", "runtime": "nope"},
    {"prompt": "x", "timeoutSeconds": 0}, {"prompt": "x", "container": {"memory": "1m"}},
    {"prompt": "x", "credentials": ["sk-live-abcdef0123456789"]}, {"prompt": "x", "credentials": "OPENAI_API_KEY"},
    {"prompt": "x\0y"},
])
def test_invalid_requests_rejected(bad):
    with pytest.raises(ValidationError):
        req = TaskRequest.from_dict(bad)
        req.task_id, req.workspace = req.task_id or "t1", req.workspace or "t1"
        req.validate()


def test_unknown_field_rejected_and_fingerprint_stable():
    with pytest.raises(ValidationError):
        TaskRequest.from_dict({"prompt": "x", "privileged": True})
    a, b = (TaskRequest(prompt="x", task_id="t", workspace="t") for _ in range(2))
    assert a.fingerprint() == b.fingerprint()
    b.model = "other"
    assert a.fingerprint() != b.fingerprint()


# ---- redaction ---------------------------------------------------------------------------------
def test_redactor_masks_values_and_common_token_shapes_but_not_task_names():
    red = Redactor(["supersecretvalue123"])
    text = "key=supersecretvalue123 Bearer abcdefghijklmnopqrstuvwx ghp_abcdefghijklmnopqrstuv sk-abcdefghijklmnopqr"
    out = red(text)
    for leaked in ("supersecretvalue123", "abcdefghijklmnopqrstuvwx", "ghp_abc", "sk-abc"):
        assert leaked not in out
    assert red("poc-task-0123456789ab-task-b1") == "poc-task-0123456789ab-task-b1"  # 'sk-' inside 'task-' is not a key


# ---- Pi output parsing / failure classification --------------------------------------------------
def feed(parser, *events, stream="stdout"):
    out = []
    for ev in events:
        out += parser.feed(stream, ev if isinstance(ev, str) else json.dumps(ev))
    return out


def assistant_end(text="", stop="stop", error=None):
    msg = {"role": "assistant", "content": [{"type": "text", "text": text}] if text else [], "stopReason": stop,
           "usage": {"input": 10, "output": 5, "totalTokens": 15}}
    if error:
        msg["errorMessage"] = error
    return {"type": "message_end", "message": msg}


def test_pi_success_run_emits_normalized_events_and_summary():
    p = PiParser()
    events = feed(p, {"type": "session", "id": "s"}, {"type": "agent_start"},
                  {"type": "tool_execution_start", "toolName": "write", "args": {"path": "hello.py"}},
                  {"type": "tool_execution_end", "toolName": "write", "isError": False, "durationMs": 2,
                   "result": {"content": [{"type": "text", "text": "ok"}]}},
                  assistant_end("done"), {"type": "agent_end", "messages": []}, {"type": "agent_settled", "aborted": False})
    kinds = [(t, d.get("kind")) for t, d in events]
    assert kinds[0] == ("agent.running", None) and ("agent.output", "tool_call") in kinds and ("agent.output", "tool_result") in kinds
    assert p.classify(0) is None
    s = p.summary()
    assert s["finalMessage"] == "done" and s["toolCalls"] == 1 and s["usage"]["totalTokens"] == 15


@pytest.mark.parametrize("error, code", [
    ("404 model 'nope:1b' not found", "model_error"),
    ("401 Unauthorized: invalid API key", "auth_error"),
    ("No API key found for anthropic", "auth_error"),
    ("Connection error.: connect ECONNREFUSED 127.0.0.1:11434", "provider_unreachable"),
    ("something odd happened", "agent_error"),
])
def test_pi_error_messages_are_classified(error, code):
    p = PiParser()
    feed(p, {"type": "agent_start"}, assistant_end(stop="error", error=error), {"type": "agent_end"})
    f = p.classify(0)
    assert f is not None and f.code == code


def test_pi_exit_zero_without_agent_run_is_failure_and_nonzero_exit_is_failure():
    p = PiParser()
    feed(p, {"type": "session"})
    assert p.classify(0).code == "no_result"
    p = PiParser()
    feed(p, "Error: Unknown provider \"x\"", stream="stderr")
    assert p.classify(1).code == "agent_error" and "Unknown provider" in p.classify(1).message


def test_pi_oversized_agent_end_line_still_counts():
    p = PiParser()
    feed(p, {"type": "agent_start"}, assistant_end("fine"))
    p.feed("stdout", '{"type":"agent_end","messages":[{"role":"system","content":"' + "x" * 50)  # truncated JSON
    assert p.classify(0) is None


def test_pi_runtime_builds_launch_plan_without_secrets_or_prompt_in_args():
    req = TaskRequest(prompt="SECRET-PROMPT-TEXT", task_id="t1", workspace="t1", model="qwen3-vl:8b")
    plan = get_runtime("pi").prepare(req)
    assert "SECRET-PROMPT-TEXT" not in " ".join(plan.args) and plan.files["prompt.txt"] == "SECRET-PROMPT-TEXT"
    assert plan.needs_llm_relay and plan.allowed_models == ("qwen3-vl:8b",) and not plan.secrets
    models = json.loads(plan.files["models.json"])["providers"]["ollama"]
    assert models["baseUrl"].startswith("http://127.0.0.1:") and models["apiKey"] == "ollama"
    other = PiRuntime().prepare(TaskRequest(prompt="x", task_id="t", workspace="t", provider="anthropic", model="m"))
    assert not other.needs_llm_relay and "models.json" not in other.files


# ---- workspace ------------------------------------------------------------------------------------
def test_resolve_workspace_confines_to_root_and_rejects_symlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    assert ws.resolve_workspace(root, "task-001") == (root / "task-001").resolve()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "evil").symlink_to(outside)
    for bad in ("evil", "../outside", "a/b", ".tasks", ""):
        with pytest.raises(ws.WorkspaceError):
            ws.resolve_workspace(root, bad)


def test_collect_changes_added_modified_deleted_with_unified_diff(tmp_path):
    d, base = tmp_path / "w", tmp_path / "base"
    d.mkdir()
    (d / "keep.py").write_text("same\n")
    (d / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (d / "gone.txt").write_text("bye\n")
    (d / "node_modules").mkdir()
    (d / "node_modules" / "x.js").write_text("ignored")
    ws.snapshot_baseline(d, base)
    (d / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (d / "gone.txt").unlink()
    (d / "hello.py").write_text("print('hi')\n")
    (d / "node_modules" / "x.js").write_text("changed but ignored")
    ch = ws.collect_changes(d, base)
    assert {(a["path"], a["status"]) for a in ch["artifacts"]} == {
        ("calc.py", "modified"), ("gone.txt", "deleted"), ("hello.py", "added")}
    assert ch["counts"] == {"added": 1, "modified": 1, "deleted": 1}
    assert "-    return a - b" in ch["diff"] and "+    return a + b" in ch["diff"] and "+print('hi')" in ch["diff"]
    assert "diff --git a/calc.py b/calc.py" in ch["diff"] and "deleted file mode" in ch["diff"]


def test_collect_changes_never_follows_symlinks_or_blocks_on_fifos(tmp_path):
    secret = tmp_path / "host-secret.txt"
    secret.write_text("TOP-SECRET-HOST-DATA")
    d, base = tmp_path / "w", tmp_path / "base"
    d.mkdir()
    ws.snapshot_baseline(d, base)
    (d / "link").symlink_to(secret)
    (d / "dirlink").symlink_to(tmp_path)
    os.mkfifo(d / "pipe")
    (d / "real.txt").write_text("ok\n")
    done = {}
    t = threading.Thread(target=lambda: done.update(ch=ws.collect_changes(d, base)))
    t.start()
    t.join(5)
    assert not t.is_alive(), "collect_changes blocked on a FIFO"
    ch = done["ch"]
    types = {a["path"]: a["type"] for a in ch["artifacts"]}
    assert types["link"] == "symlink" and types["dirlink"] == "symlink" and types["real.txt"] == "file"
    assert "pipe" not in types and "TOP-SECRET-HOST-DATA" not in ch["diff"]


def test_tree_size_stops_early_and_remove_tree_repairs_permissions(tmp_path):
    d = tmp_path / "w"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "big").write_bytes(b"x" * 5000)
    assert ws.tree_size(d, 100) > 100 and ws.tree_size(d, 10**9) == 5000
    os.chmod(d / "sub", 0)  # an agent can leave directories unreadable
    ws.remove_tree(d)
    assert not d.exists()


# ---- model relay ------------------------------------------------------------------------------------
class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _Upstream.seen.append(("GET", self.path))
        self._send(200, b'{"data":[{"id":"allowed-model"}]}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Upstream.seen.append(("POST", self.path, body.get("model")))
        if body.get("model") == "missing-model":
            return self._send(404, b'{"error":{"message":"model \\"missing-model\\" not found"}}')
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for part in (b"data: one\n\n", b"data: [DONE]\n\n"):
            self.wfile.write(b"%x\r\n" % len(part) + part + b"\r\n")
            self.wfile.flush()
            time.sleep(0.05)
        self.wfile.write(b"0\r\n\r\n")


class _UnixConn(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(self._path)


@pytest.fixture
def relay(tmp_path_factory):
    _Upstream.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    r = LLMRelay(tmp_path_factory.mktemp("sock"), ["allowed-model", "missing-model"], upstream=("127.0.0.1", srv.server_address[1])).start()
    yield r
    r.stop()
    srv.shutdown()


def call(relay, method, path, body=None):
    c = _UnixConn(str(relay.path))
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers={"Content-Type": "application/json"})
    resp = c.getresponse()
    data = resp.read()
    c.close()
    return resp.status, data


def test_relay_forwards_allowed_routes_and_streams(relay):
    status, data = call(relay, "POST", "/v1/chat/completions", {"model": "allowed-model", "stream": True})
    assert status == 200 and data == b"data: one\n\ndata: [DONE]\n\n"
    assert call(relay, "GET", "/v1/models")[0] == 200
    assert ("POST", "/v1/chat/completions", "allowed-model") in _Upstream.seen


@pytest.mark.parametrize("method, path, body", [
    ("POST", "/v1/chat/completions", {"model": "other-model"}),  # model not configured for this task
    ("POST", "/api/pull", {"model": "allowed-model"}),
    ("POST", "/api/delete", {"model": "allowed-model"}),
    ("DELETE", "/api/delete", None),
    ("GET", "/api/tags", None),
    ("POST", "/v1/embeddings", {"model": "allowed-model"}),
])
def test_relay_refuses_everything_else_without_touching_upstream(relay, method, path, body):
    status, _ = call(relay, method, path, body)
    assert status in (403, 405)
    assert _Upstream.seen == [] and relay.denied


def test_relay_records_upstream_errors_and_unreachable_backend(relay):
    status, _ = call(relay, "POST", "/v1/chat/completions", {"model": "missing-model"})
    assert status == 404 and relay.upstream_errors[0][0] == 404 and "not found" in relay.upstream_errors[0][1]
    relay.upstream = ("127.0.0.1", 1)  # nothing listens there
    status, _ = call(relay, "POST", "/v1/chat/completions", {"model": "allowed-model"})
    assert status == 502 and relay.unreachable


# ---- container spec ---------------------------------------------------------------------------------
def test_task_spec_adds_only_explicit_mounts_and_keeps_hardening():
    spec = ContainerSpec(
        name="n", image="img", labels={}, env={}, cpus=2, memory_mb=2048, pids_limit=512, workspace_tmpfs=False,
        mounts=(("/srv/ws/task-1", "/workspace", False), ("/srv/ws/.tasks/task-1/runtime", "/run/task", True)),
        tmpfs=(("/tmp", "rw,noexec,nosuid,nodev,size=256m"),), user="1000:1000", args=("--mode", "json"), entrypoint="/bin/x")
    args = build_create_args(spec)
    assert args[args.index("--network") + 1] == "none" and "--read-only" in args and "--privileged" not in args
    assert args[args.index("--cap-drop") + 1] == "ALL" and args[args.index("--user") + 1] == "1000:1000"
    assert args[args.index("--security-opt") + 1] == "no-new-privileges=true"
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    assert mounts == ["type=bind,src=/srv/ws/task-1,dst=/workspace", "type=bind,src=/srv/ws/.tasks/task-1/runtime,dst=/run/task,readonly"]
    assert not any(a.startswith("/workspace:") for a in args), "tmpfs workspace must be replaced by the bind mount"
    assert args[-2:] == ["--mode", "json"] and args[args.index("--entrypoint") + 1] == "/bin/x"
    assert not any("docker.sock" in a or a in ("--pid", "--ipc", "--device") for a in args)


@pytest.mark.parametrize("bad", ["/a,b", "relative/path", '/a"b', "/a\nb"])
def test_unsafe_bind_mount_paths_refused(bad):
    spec = ContainerSpec(name="n", image="i", labels={}, env={}, cpus=1, memory_mb=256, pids_limit=64,
                         mounts=((bad, "/workspace", False),))
    with pytest.raises(DockerError):
        build_create_args(spec)


def test_pending_state_machine_and_phase1_defaults_unchanged():
    sm = StateMachine()
    for s in (S.PREPARING, S.STARTING, S.RUNNING, S.COMPLETED, S.CLEANUP, S.REMOVED):
        sm.transition(s)
    plain = ContainerSpec(name="n", image="i", labels={}, env={}, cpus=0.5, memory_mb=256, pids_limit=64)
    args = build_create_args(plain)
    assert "--mount" not in args and "--entrypoint" not in args
    assert args[args.index("--tmpfs") + 1].startswith("/workspace:rw,noexec,nosuid,nodev,size=16m")


# ---- Phase 1 sweep must not destroy Phase 2 task containers ------------------------------------------
def test_phase1_cleanup_stale_skips_task_containers_but_still_removes_plain_orphans(tmp_path):
    fake = FakeDocker()
    with Orchestrator(docker=fake, workspace_root=tmp_path) as orch:
        task = fake.add_foreign({LABEL_PROJECT: "phase1", LABEL_RUN_ID: "deadrun", LABEL_KIND: KIND_TASK}, "task")
        plain = fake.add_foreign({LABEL_PROJECT: "phase1", LABEL_RUN_ID: "deadrun"}, "plain")
        report = orch.cleanup_stale()
    assert report["removed_containers"] == [plain] and report["skipped_task_containers"] == [task]
    assert task in fake.containers
