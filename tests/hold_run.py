"""Helper process for orphan-recovery tests: an orchestrator that starts one long agent and then idles.

Usage: python hold_run.py <workspace_root>   (prints "<run_id> <container_id>" when ready)
Killing it with SIGKILL leaves the container behind, simulating an interrupted run.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import Orchestrator, TaskConfig  # noqa: E402

orch = Orchestrator(workspace_root=sys.argv[1])
agent_id = orch.create_agent(TaskConfig(type="sleep", params={"duration": 120}, timeout_s=300))
orch.start_agent(agent_id)
print(orch.run_id, orch.get_agent_status(agent_id)["container_id"], flush=True)
time.sleep(600)
