"""Mock Agent executed inside an isolated container.

Contract (Phase 1):
  * Task arrives as JSON in the TASK_JSON environment variable:
        {"task_id": "...", "type": "echo|sleep|compute|fail", "params": {...}}
  * The structured result is written to $WORKSPACE/result.json and printed on
    stdout as a single line prefixed with RESULT_PREFIX. The workspace is a
    tmpfs that disappears with the container, so the orchestrator collects the
    stdout line before destroying the container.
  * Exit codes: 0 = completed, 1 = task failed, 2 = invalid task / internal error.

No LLM, network or credentials are used. Phase 2 replaces `run_task`.
"""
import json
import math
import os
import sys
import time

RESULT_PREFIX = "@@AGENT_RESULT@@"
EXIT_OK, EXIT_TASK_FAILED, EXIT_INVALID = 0, 1, 2
MAX_SLEEP_S = 3600
MAX_COMPUTE_N = 5_000_000


class TaskError(Exception):
    def __init__(self, message, exit_code=EXIT_TASK_FAILED, error_type="task_failed"):
        super().__init__(message)
        self.exit_code = exit_code
        self.error_type = error_type


def _number(params, key, default, minimum, maximum):
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TaskError(f"param '{key}' must be a number", EXIT_INVALID, "invalid_task")
    if not (minimum <= value <= maximum):
        raise TaskError(
            f"param '{key}' must be between {minimum} and {maximum}", EXIT_INVALID, "invalid_task"
        )
    return value


def run_task(task):
    """Execute a mock task and return its output dict."""
    kind, params = task["type"], task.get("params") or {}
    if not isinstance(params, dict):
        raise TaskError("'params' must be an object", EXIT_INVALID, "invalid_task")

    if kind == "echo":
        return {"message": str(params.get("message", ""))}
    if kind == "sleep":
        duration = _number(params, "duration", 1, 0, MAX_SLEEP_S)
        time.sleep(duration)
        return {"slept_s": duration}
    if kind == "compute":
        n = int(_number(params, "n", 100_000, 1, MAX_COMPUTE_N))
        # Count primes <= n with a sieve: deterministic CPU work.
        sieve = bytearray([1]) * (n + 1)
        sieve[0] = sieve[1] = 0
        for i in range(2, math.isqrt(n) + 1):
            if sieve[i]:
                sieve[i * i :: i] = bytearray(len(range(i * i, n + 1, i)))
        return {"n": n, "primes_up_to_n": sum(sieve)}
    if kind == "fail":
        code = int(_number(params, "exit_code", 1, 1, 255))
        raise TaskError(str(params.get("message", "intentional failure")), code)
    raise TaskError(f"unsupported task type: {kind!r}", EXIT_INVALID, "invalid_task")


def emit(result, workspace):
    payload = json.dumps(result, separators=(",", ":"))
    try:
        with open(os.path.join(workspace, "result.json"), "w", encoding="utf-8") as fh:
            fh.write(payload)
    except OSError as exc:
        print(f"warning: could not write workspace result: {exc}", file=sys.stderr)
    print(RESULT_PREFIX + payload, flush=True)


def main():
    workspace = os.environ.get("WORKSPACE", "/workspace")
    agent_id = os.environ.get("AGENT_ID")
    started = time.monotonic()
    task_id, exit_code, output, error, status = None, EXIT_OK, None, None, "completed"
    try:
        try:
            task = json.loads(os.environ["TASK_JSON"])
            task_id = task["task_id"]
            task["type"]
        except (KeyError, TypeError, ValueError) as exc:
            raise TaskError(f"invalid task JSON: {exc!r}", EXIT_INVALID, "invalid_task")
        output = run_task(task)
    except TaskError as exc:
        status, exit_code = "failed", exc.exit_code
        error = {"type": exc.error_type, "message": str(exc)}
    except Exception as exc:  # unexpected agent bug: still report structurally
        status, exit_code = "failed", EXIT_INVALID
        error = {"type": "internal_error", "message": repr(exc)}

    emit(
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "status": status,
            "exit_code": exit_code,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "output": output,
            "error": error,
        },
        workspace,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
