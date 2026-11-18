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

    p = sub.add_parser("build", help="build the agent image (mock by default; --runtime pi for the real Pi agent)")
    p.add_argument("--runtime", choices=["mock", "pi"], default="mock")
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

    p = sub.add_parser("task", help="Phase 2: run real coding-agent tasks (default runtime: pi)")
    tsub = p.add_subparsers(dest="task_command", required=True)

    def _t(name, help_, tid=True):
        q = tsub.add_parser(name, help=help_)
        q.add_argument("--workspace-root", default=os.environ.get("MULTIAGENT_WORKSPACES", "workspaces"))
        q.add_argument("--max-concurrency", type=int, default=2)
        if tid:
            q.add_argument("task_id")
        return q

    q = _t("run", "submit a task, stream its events (stderr) and print the normalized result (stdout)", tid=False)
    q.add_argument("--config", help="JSON task request (flags override); accepts nested agent/container objects")
    q.add_argument("--prompt")
    q.add_argument("--prompt-file")
    q.add_argument("--task-id")
    q.add_argument("--workspace", help="logical workspace id (default: the task id)")
    q.add_argument("--runtime", default=None)
    q.add_argument("--provider", default=None)
    q.add_argument("--model", default=None)
    q.add_argument("--timeout", type=float, default=None, help="seconds (default 900)")
    q.add_argument("--memory", default=None, help="e.g. 2g, 512m")
    q.add_argument("--cpus", type=float, default=None)
    q.add_argument("--pids", type=int, default=None)
    q.add_argument("--credential", action="append", default=[], metavar="ENV_NAME",
                   help="inject this orchestrator environment variable into the task as a read-only secret file")
    q.add_argument("--quiet", action="store_true", help="do not print events")
    _t("get", "show task state and result")
    q = _t("events", "show recorded events")
    q.add_argument("--follow", action="store_true")
    _t("result", "print the normalized result of a finished task")
    _t("cancel", "request cancellation (works across processes)")
    _t("list", "list tasks", tid=False)
    q = _t("reconcile", "adopt/finalise task containers left by a dead orchestrator; --wait for adopted tasks", tid=False)
    q.add_argument("--wait", action="store_true")
    _t("cleanup-workspace", "delete a task workspace directory (artifacts); separate from container removal").add_argument(
        "--yes", action="store_true", required=True)
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
    if args.runtime == "pi":
        from .runtimes.pi import DEFAULT_IMAGE as PI_IMAGE

        image = PI_IMAGE if args.image == DEFAULT_IMAGE else args.image
        # The container runs as the host uid so the bind-mounted task workspace is writable and owned by the caller.
        DockerClient().build_image(image, str(REPO_ROOT / "agent" / "pi"),
                                   {"AGENT_UID": os.getuid() or 10001, "AGENT_GID": os.getgid() or 10001})
        print(f"built {image} (uid {os.getuid() or 10001})")
        return 0
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


def _print_event(ev: dict) -> None:
    d = ev["data"]
    detail = d.get("text") or d.get("tool") or d.get("error") or d.get("status") or ""
    if isinstance(detail, dict):
        detail = f"{detail.get('kind')}/{detail.get('code')}: {detail.get('message')}"
    print(f"{ev['timestamp'][11:23]} {ev['type']:20} {str(detail)[:160]!s}", file=sys.stderr, flush=True)


def _task_request(args) -> dict:
    data = json.loads(Path(args.config).read_text()) if args.config else {}
    if args.prompt_file:
        data["prompt"] = Path(args.prompt_file).read_text()
    for flag, key in (("prompt", "prompt"), ("task_id", "taskId"), ("workspace", "workspace"), ("runtime", "runtime"),
                      ("provider", "provider"), ("model", "model"), ("timeout", "timeoutSeconds"),
                      ("memory", "memory"), ("cpus", "cpus"), ("pids", "pids")):
        value = getattr(args, flag)
        if value is not None:
            data[key] = value
    if args.credential:
        data["credentials"] = list(args.credential)
    return data


def cmd_task(args) -> int:
    from .tasks import TaskConflict, TaskManager, TaskNotFinished, TaskNotFound, WorkspaceBusy

    cmd = args.task_command
    try:
        with TaskManager(workspace_root=args.workspace_root, max_concurrency=args.max_concurrency) as tm:
            if cmd == "run":
                task = tm.submit_task(_task_request(args))
                tid = task["taskId"]
                try:
                    for ev in tm.stream_events(tid):
                        if not args.quiet:
                            _print_event(ev)
                    result = tm.get_task_result(tid)
                except KeyboardInterrupt:
                    tm.cancel_task(tid)
                    tm.wait(tid, 60)
                    result = tm.get_task_result(tid)
                print(json.dumps(result, indent=2))
                return 0 if result["status"] == "completed" else 1
            if cmd == "get":
                print(json.dumps(tm.get_task(args.task_id), indent=2))
            elif cmd == "events":
                events = tm.stream_events(args.task_id) if args.follow else tm.get_task_events(args.task_id)
                for ev in events:
                    print(json.dumps(ev), flush=True)
            elif cmd == "result":
                print(json.dumps(tm.get_task_result(args.task_id), indent=2))
            elif cmd == "cancel":
                print(json.dumps(tm.cancel_task(args.task_id), indent=2))
            elif cmd == "list":
                for t in tm.list_tasks():
                    print(f"{t['taskId']:<24}{t['state']:<10}{str(t['status']):<11}{t['runtime']:<6}{t['workspace']}")
            elif cmd == "reconcile":
                report = tm.reconcile()
                print(json.dumps(report, indent=2))
                if args.wait:
                    for tid in report["adopted"]:
                        tm.wait(tid)
                        print(json.dumps({tid: tm.get_task(tid)["status"]}))
            elif cmd == "cleanup-workspace":
                ok = tm.cleanup_workspace(args.task_id)
                print("removed" if ok else "workspace is in use by an active task")
                return 0 if ok else 1
    except TaskNotFound as exc:
        print(f"error: unknown task {exc}", file=sys.stderr)
        return 2
    except (TaskConflict, WorkspaceBusy, TaskNotFinished) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


COMMANDS = {"build": cmd_build, "run": cmd_run, "demo": cmd_demo, "status": cmd_status, "cleanup": cmd_cleanup,
            "task": cmd_task}


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
    except BrokenPipeError:  # e.g. `... events <id> | head`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except (ValidationError, DockerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
