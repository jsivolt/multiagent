"""Runtime adapter interface.

The core (tasks.py) owns everything generic: container lifecycle, timeouts, cancellation, workspace,
events, persistence, cleanup. A runtime adapter only knows how to run ONE coding-agent CLI inside the
container and how to read its output. Mapping to the abstract interface:

    prepare(config)    -> AgentRuntime.prepare()        builds the LaunchPlan (image, args, files, secrets)
    start/cancel/status-> TaskManager                    same for every runtime (docker create/start/stop)
    streamEvents()     -> OutputParser.feed()           raw stdout/stderr line -> normalized events
    collectResult()    -> OutputParser.summary()/classify()   final output + failure classification

Adding a runtime = one module implementing AgentRuntime/OutputParser and a `register()` call.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LaunchPlan:
    image: str
    args: list  # arguments for the image entrypoint
    env: dict = field(default_factory=dict)  # non-secret environment (visible in `docker inspect`)
    files: dict = field(default_factory=dict)  # name -> text, mounted read-only at /run/task
    secrets: dict = field(default_factory=dict)  # ENV_NAME -> value, mounted read-only at /run/secrets
    needs_llm_relay: bool = False  # model API reached through the host-side filtered relay socket
    allowed_models: tuple = ()  # models the relay will forward requests for
    required_image_uid_label: Optional[str] = None  # image label that must equal the container uid
    entrypoint: Optional[str] = None  # override the image entrypoint (adapter-controlled, e.g. diagnostics)


@dataclass
class Failure:
    code: str  # e.g. model_error, auth_error, agent_error, no_result
    message: str


class OutputParser(ABC):
    """Stateful, memory-bounded parser for one task's output."""

    @abstractmethod
    def feed(self, stream: str, line: str) -> list:
        """Return [(event_type, data_dict)] for one output line ('stdout' | 'stderr')."""

    @abstractmethod
    def classify(self, exit_code: Optional[int]) -> Optional[Failure]:
        """None if the agent run succeeded, otherwise why it failed (called after the process exited)."""

    @abstractmethod
    def summary(self) -> dict:
        """Runtime-specific result details (final message, usage, ...)."""


class AgentRuntime(ABC):
    name: str
    default_image: str

    @abstractmethod
    def prepare(self, request) -> LaunchPlan:
        """Validate runtime-specific options and describe how to launch the CLI for this task."""

    @abstractmethod
    def new_parser(self) -> OutputParser:
        ...


_REGISTRY: dict = {}


def register(runtime: AgentRuntime) -> None:
    _REGISTRY[runtime.name] = runtime


def get_runtime(name: str) -> AgentRuntime:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown runtime {name!r}; available: {sorted(_REGISTRY)}") from None


def available_runtimes() -> list:
    return sorted(_REGISTRY)
