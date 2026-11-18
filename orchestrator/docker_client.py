"""Thin wrapper around the `docker` CLI (talks to the Docker Engine API via the daemon).

Using the CLI keeps the orchestrator dependency-free. The wrapper is the only place
that builds docker commands, so the hardening flags live in `build_create_args`.
"""
from __future__ import annotations

import json
import subprocess
from typing import Optional

from .models import ContainerSpec, WORKSPACE_PATH


class DockerError(RuntimeError):
    def __init__(self, message, *, stderr="", unavailable=False, not_found=False):
        super().__init__(message)
        self.stderr = stderr
        self.unavailable = unavailable  # daemon/CLI unreachable
        self.not_found = not_found


def build_create_args(spec: ContainerSpec) -> list:
    """`docker create` argv for one agent container. All isolation settings are fixed here."""
    mem = f"{spec.memory_mb}m"
    args = [
        "create",
        "--name", spec.name,
        "--init",  # reap zombies / forward signals
        "--network", "none",
        "--read-only",
        "--tmpfs", (
            f"{WORKSPACE_PATH}:rw,noexec,nosuid,nodev,"
            f"size={spec.workspace_size_mb}m,uid={spec.uid},gid={spec.uid},mode=0700"
        ),
        "--workdir", WORKSPACE_PATH,
        "--user", f"{spec.uid}:{spec.uid}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true",  # default seccomp profile stays applied
        "--cpus", str(spec.cpus),
        "--memory", mem,
        "--memory-swap", mem,  # no swap
        "--pids-limit", str(spec.pids_limit),
        "--stop-timeout", "2",
        "--restart", "no",
        "--log-driver", "json-file",
        "--log-opt", "max-size=1m",
        "--log-opt", "max-file=1",
    ]
    for key, value in spec.labels.items():
        args += ["--label", f"{key}={value}"]
    for key, value in spec.env.items():
        args += ["--env", f"{key}={value}"]
    args.append(spec.image)
    return args


class DockerClient:
    def __init__(self, binary: str = "docker", default_timeout: float = 60.0):
        self.binary = binary
        self.default_timeout = default_timeout

    # -- plumbing ---------------------------------------------------------------
    def _run(self, args, *, timeout: Optional[float] = None, check: bool = True):
        cmd = [self.binary, *args]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=timeout or self.default_timeout
            )
        except FileNotFoundError as exc:
            raise DockerError(f"docker CLI not found: {exc}", unavailable=True)
        except subprocess.TimeoutExpired:
            raise DockerError(f"docker {args[0]} timed out")
        if check and proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace").strip()
            raise DockerError(
                f"docker {' '.join(args[:2])} failed: {stderr}",
                stderr=stderr,
                unavailable="cannot connect to the docker daemon" in stderr.lower(),
                not_found="no such" in stderr.lower(),
            )
        return proc

    # -- images -----------------------------------------------------------------
    def image_exists(self, image: str) -> bool:
        return self._run(["image", "inspect", image], check=False).returncode == 0

    def build_image(self, tag: str, context_dir: str) -> None:
        self._run(["build", "-t", tag, context_dir], timeout=600)

    # -- containers ---------------------------------------------------------------
    def create_container(self, spec: ContainerSpec) -> str:
        return self._run(build_create_args(spec)).stdout.decode().strip()

    def start_container(self, container_id: str) -> None:
        self._run(["start", container_id])

    def inspect_container(self, container_id: str) -> dict:
        out = self._run(["inspect", "--type", "container", container_id]).stdout
        return json.loads(out)[0]

    def stop_container(self, container_id: str, grace_s: int = 2) -> None:
        try:
            self._run(["stop", "-t", str(grace_s), container_id], timeout=grace_s + 30)
        except DockerError as exc:
            if not exc.not_found:
                raise

    def kill_container(self, container_id: str) -> None:
        try:
            self._run(["kill", container_id])
        except DockerError as exc:
            if not (exc.not_found or "is not running" in exc.stderr):
                raise

    def remove_container(self, container_id: str) -> None:
        try:
            self._run(["rm", "-f", "-v", container_id])
        except DockerError as exc:
            if not exc.not_found:
                raise

    def logs(self, container_id: str, tail: int = 200):
        proc = self._run(["logs", "--tail", str(tail), container_id])
        return proc.stdout.decode(errors="replace"), proc.stderr.decode(errors="replace")

    def list_containers(self, labels: dict) -> list:
        """Full `docker inspect` documents of all containers (any state) matching labels."""
        args = ["ps", "-aq", "--no-trunc"]
        for key, value in labels.items():
            args += ["--filter", f"label={key}={value}"]
        ids = self._run(args).stdout.decode().split()
        if not ids:
            return []
        proc = self._run(["inspect", "--type", "container", *ids], check=False)
        try:
            return json.loads(proc.stdout)  # containers removed meanwhile are simply absent
        except ValueError:
            return []
