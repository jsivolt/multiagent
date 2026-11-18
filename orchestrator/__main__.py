"""CLI: python -m orchestrator {build,run,demo,status,cleanup}"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

from .docker_client import DockerClient, DockerError
from .manager import Orchestrator
from .models import DEFAULT_IMAGE, TaskConfig, ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--workspace-root", default=os.environ.get("MULTIAGENT_WORKSPACES", "workspaces"),
                   help="host directory for results and run leases (default: ./workspaces)")
    p.add_argument("--image", default=DEFAULT_IMAGE, help="agent image to run")
    p.add_argument("--max-concurrency", type=int, default=4, help="max containers running at once")


def _limits(p: argparse.ArgumentParser) -> None:
    p.add_argument("--timeout", type=float, default=30.0, help="execution timeout in seconds")
    p.add_argument("--cpus", type=float, default=0.5)
    p.add_argument("--memory-mb", type=int, default=256)
    p.add_argument("--pids", type=int, default=64)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m orchestrator", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="build the mock agent image")
    _common(p)

    p = sub.add_parser("run", help="run a single task in a fresh container")
    _common(p)
    _limits(p)
    p.add_argument("--type", required=True, choices=["echo", "sleep", "compute", "fail"])
    p.add_argument("--message", help="echo/fail: message")
    p.add_argument("--duration", type=float, help="sleep: seconds")
    p.add_argument("--n", type=int, help="compute: count primes up to n")
    p.add_argument("--exit-code", type=int, help="fail: exit code (1-255)")
    p.add_argument("--task-id")

    p = sub.add_parser("demo", help="run several tasks concurrently")
    _common(p)
    _limits(p)
    p.add_argument("--agents", type=int, default=3)
    p.add_argument("--duration", type=float, default=2.0, help="sleep seconds per agent")
    p.add_argument("--with-failure", action="store_true", help="make the last agent fail intentionally")

    p = sub.add_parser("status", help="list containers managed by this POC")
    _common(p)

    p = sub.add_parser("cleanup", help="remove containers left by dead orchestrator runs")
    _common(p)
    p.add_argument("--stale", action="store_true", required=True,
                   help="remove containers of stale runs only (active runs are preserved)")
    return parser


def _make(args, **kw) -> Orchestrator:
    return Orchestrator(
        workspace_root=args.workspace_root, max_concurrency=args.max_concurrency, **kw
    )


def _config(args, type_, params) -> TaskConfig:
    return TaskConfig(
        type=type_, params=params, task_id=getattr(args, "task_id", None),
        timeout_s=args.timeout, cpus=args.cpus, memory_mb=args.memory_mb,
        pids_limit=args.pids, image=args.image,
    )


def _max_overlap(results) -> int:
    events = []
    for r in results:
        if r.started_at and r.finished_at:
            events += [(datetime.fromisoformat(r.started_at), 1), (datetime.fromisoformat(r.finished_at), -1)]
    level = peak = 0
    for _, delta in sorted(events, key=lambda e: (e[0], e[1])):
        level += delta
        peak = max(peak, level)
    return peak


def cmd_build(args) -> int:
    DockerClient().build_image(args.image, str(REPO_ROOT / "agent"))
    print(f"built {args.image}")
    return 0


def cmd_run(args) -> int:
    params = {}
    for key, value in (("message", args.message), ("duration", args.duration),
                       ("n", args.n), ("exit_code", args.exit_code)):
        if value is not None:
            params[key] = value
    with _make(args, cleanup_stale_on_start=True) as orch:
        result = orch.run_agent(_config(args, args.type, params))
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.status == "completed" else 1


def cmd_demo(args) -> int:
    configs = []
    for i in range(args.agents):
        if args.with_failure and i == args.agents - 1:
            configs.append(_config(args, "fail", {"message": "intentional demo failure"}))
        else:
            configs.append(_config(args, "sleep", {"duration": args.duration}))
    with _make(args, cleanup_stale_on_start=True) as orch:
        results = orch.run_many(configs)
        leaked = [r for r in orch.list_managed() if r["owner"] == "this-run"]
    print(json.dumps(
        {
            "results": [r.to_dict() for r in results],
            "max_observed_overlap": _max_overlap(results),
            "max_concurrency": args.max_concurrency,
            "leaked_containers": len(leaked),
        },
        indent=2,
    ))
    return 0 if all(r.status == "completed" for r in results) or args.with_failure else 1


def cmd_status(args) -> int:
    with _make(args) as orch:
        rows = orch.list_managed()
    if not rows:
        print("no managed containers")
        return 0
    print(f"{'CONTAINER':<13}{'NAME':<34}{'STATE':<10}{'OWNER':<10}RUN")
    for r in rows:
        print(f"{r['container_id']:<13}{r['name']:<34}{r['docker_state']:<10}{r['owner']:<10}{r['run_id']}")
    return 0


def cmd_cleanup(args) -> int:
    with _make(args) as orch:
        print(json.dumps(orch.cleanup_stale(), indent=2))
    return 0


COMMANDS = {"build": cmd_build, "run": cmd_run, "demo": cmd_demo, "status": cmd_status, "cleanup": cmd_cleanup}


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    # Turn SIGTERM into KeyboardInterrupt so `with Orchestrator` always cleans up containers.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        return COMMANDS[args.command](args)
    except KeyboardInterrupt:
        print("interrupted; containers cleaned up", file=sys.stderr)
        return 130
    except (ValidationError, DockerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
