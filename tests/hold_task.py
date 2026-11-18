"""Helper process for crash-recovery tests: starts a task, prints "<task_id> <run_id> <container_id>" once it is
running, then idles until killed. Killing it with SIGKILL simulates an unexpected orchestrator restart.

Usage: python hold_task.py <workspace_root> <request-json>"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe import register_probe  # noqa: E402
from orchestrator.tasks import TaskManager  # noqa: E402

register_probe()
tm = TaskManager(workspace_root=sys.argv[1])
task = tm.submit_task(json.loads(sys.argv[2]))
tid = task["taskId"]
deadline = time.time() + 120
while time.time() < deadline:
    t = tm.get_task(tid)
    if t["state"] == "RUNNING" and t["containerId"] and any(
            e["type"] in ("agent.running",) or json.loads(sys.argv[2]).get("runtime") == "probe"
            for e in tm.get_task_events(tid)):
        break
    time.sleep(0.2)
print(tid, tm.run_id, tm.get_task(tid)["containerId"], flush=True)
time.sleep(900)
