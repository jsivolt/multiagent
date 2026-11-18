"""Pi coding agent (https://pi.dev, npm: @earendil-works/pi-coding-agent) adapter.

Runs `pi --mode json` non-interactively inside the container and translates Pi's JSONL event stream
(docs/json.md in the Pi package) into normalized orchestrator events.
"""
from __future__ import annotations

import json
import re
from collections import deque
from typing import Optional

from .base import AgentRuntime, Failure, LaunchPlan, OutputParser

PI_VERSION = "1.5.2"
DEFAULT_IMAGE = "multiagent-poc-pi:phase2"
DEFAULT_MODEL = "qwen3-vl:8b"
LLM_PORT = 11434

_AUTH_RE = re.compile(r"api[ _-]?key|unauthori[sz]ed|authenticat|\b40[13]\b|forbidden|credential|not logged in|no auth", re.I)
_MODEL_RE = re.compile(r"model\b.{0,80}(not found|does not exist|unknown|not available|no such)|unknown model|\b404\b|no models? (available|match)|not found", re.I)
_UNREACHABLE_RE = re.compile(r"ECONNREFUSED|ENOTFOUND|fetch failed|connection (error|refused)|socket hang up|ETIMEDOUT|EAI_AGAIN", re.I)


def _trunc(text, limit):
    text = text if isinstance(text, str) else json.dumps(text, default=str)
    return text if len(text) <= limit else text[:limit] + f"…[+{len(text) - limit} chars]"


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


class PiParser(OutputParser):
    def __init__(self):
        self.started = False
        self.agent_end = False
        self.settled = False
        self.aborted = False
        self.last_text = ""
        self.last_error: Optional[str] = None
        self.retry_final_error: Optional[str] = None
        self.assistant_messages = 0
        self.tool_calls = 0
        self.tool_errors = 0
        self.usage = {"input": 0, "output": 0, "totalTokens": 0}
        self.stderr_tail = deque(maxlen=20)

    def feed(self, stream, line):
        if stream == "stderr":
            self.stderr_tail.append(line[:500])
            return [("agent.output", {"stream": "stderr", "kind": "stderr", "text": _trunc(line, 500)})]
        try:
            ev = json.loads(line)
        except ValueError:
            m = re.match(r'\{"type":"([a-z_]+)"', line)  # oversized/truncated record: keep the type only
            if m and m.group(1) == "agent_end":
                self.agent_end = True
            if not m:
                return [("agent.output", {"stream": "stdout", "kind": "raw", "text": _trunc(line, 500)})]
            return []
        if not isinstance(ev, dict):
            return []
        kind = ev.get("type")
        out = []
        if kind in ("session", "agent_start") and not self.started:
            self.started = True
            out.append(("agent.running", {}))
        if kind == "message_end":
            msg = ev.get("message") or {}
            if msg.get("role") == "assistant":
                self.assistant_messages += 1
                text = _text_of(msg.get("content"))
                stop = msg.get("stopReason")
                usage = msg.get("usage") or {}
                for key in self.usage:
                    self.usage[key] += int(usage.get(key) or 0)
                if stop in ("error", "aborted"):
                    self.last_error = msg.get("errorMessage") or f"assistant stopped: {stop}"
                else:
                    self.last_error = None
                    if text:
                        self.last_text = text
                out.append(("agent.output", {"stream": "stdout", "kind": "assistant_message",
                                             "text": _trunc(text, 2000), "stopReason": stop}))
        elif kind == "tool_execution_start":
            self.tool_calls += 1
            out.append(("agent.output", {"stream": "stdout", "kind": "tool_call", "tool": ev.get("toolName"),
                                         "args": _trunc(ev.get("args"), 500)}))
        elif kind == "tool_execution_end":
            if ev.get("isError"):
                self.tool_errors += 1
            res = ev.get("result") or {}
            out.append(("agent.output", {"stream": "stdout", "kind": "tool_result", "tool": ev.get("toolName"),
                                         "isError": bool(ev.get("isError")), "durationMs": ev.get("durationMs"),
                                         "text": _trunc(_text_of(res.get("content") if isinstance(res, dict) else res), 500)}))
        elif kind == "auto_retry_start":
            out.append(("agent.output", {"stream": "stdout", "kind": "retry", "attempt": ev.get("attempt"),
                                         "text": _trunc(ev.get("errorMessage", ""), 300)}))
        elif kind == "auto_retry_end" and not ev.get("success"):
            self.retry_final_error = ev.get("finalError") or "retries exhausted"
        elif kind == "agent_end":
            self.agent_end = True
        elif kind == "agent_settled":
            self.settled = True
            self.aborted = bool(ev.get("aborted"))
        return out

    def _error_text(self) -> str:
        return self.last_error or self.retry_final_error or " | ".join(self.stderr_tail)

    def classify(self, exit_code):
        text = self._error_text()
        if self.last_error or self.retry_final_error:
            return Failure(self._code(text), _trunc(text, 600))
        if exit_code not in (0, None):
            return Failure(self._code(text) if text else "agent_error",
                           _trunc(text or f"pi exited with code {exit_code}", 600))
        if not self.agent_end:
            return Failure("no_result", _trunc(text or "pi exited 0 without completing an agent run", 600))
        if self.aborted:
            return Failure("agent_error", "agent run was aborted")
        return None

    @staticmethod
    def _code(text: str) -> str:
        if _UNREACHABLE_RE.search(text):
            return "provider_unreachable"
        if _AUTH_RE.search(text):
            return "auth_error"
        if _MODEL_RE.search(text):
            return "model_error"
        return "agent_error"

    def summary(self):
        return {
            "finalMessage": _trunc(self.last_text, 8000),
            "assistantMessages": self.assistant_messages,
            "toolCalls": self.tool_calls,
            "toolErrors": self.tool_errors,
            "usage": dict(self.usage),
        }


class PiRuntime(AgentRuntime):
    name = "pi"
    default_image = DEFAULT_IMAGE
    default_model = DEFAULT_MODEL

    def prepare(self, request) -> LaunchPlan:
        provider = request.provider or "ollama"
        model = request.model or DEFAULT_MODEL
        opts = request.options or {}
        args = [
            "--mode", "json", "--provider", provider, "--model", model,
            "--no-session", "--no-extensions", "--no-skills", "--no-mcp", "--no-themes",
            "--no-prompt-templates", "--thinking", str(opts.get("thinking", "off")),
        ]
        files = {"prompt.txt": request.prompt}
        plan = LaunchPlan(
            image=request.image or DEFAULT_IMAGE,
            args=args,
            env={"LLM_PORT": str(LLM_PORT)},
            files=files,
            required_image_uid_label="multiagent.poc.agent_uid",
        )
        if provider == "ollama":
            # Local Ollama is reached through the orchestrator's filtered relay socket: no network, no credentials.
            context = int(opts.get("contextWindow", 32768))
            files["models.json"] = json.dumps({"providers": {"ollama": {
                "baseUrl": f"http://127.0.0.1:{LLM_PORT}/v1",
                "api": "openai-completions",
                "apiKey": "ollama",  # dummy value required by Pi; Ollama ignores it
                "models": [{"id": model, "name": model, "contextWindow": context,
                            "maxTokens": int(opts.get("maxTokens", 8192)), "reasoning": False}],
            }}}, indent=2)
            plan.needs_llm_relay = True
            plan.allowed_models = (model,)
        return plan

    def new_parser(self):
        return PiParser()
