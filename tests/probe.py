"""Test-only runtime adapter: runs an arbitrary entrypoint/args inside the real Pi image through the real
TaskManager container spec (same user, mounts, tmpfs, limits, labels, lifecycle). Used to make live Docker
tests deterministic (no LLM): the container stays alive so tests can `docker exec` probes into it."""
from orchestrator.runtimes import register
from orchestrator.runtimes.base import AgentRuntime, Failure, LaunchPlan, OutputParser
from orchestrator.runtimes.pi import DEFAULT_IMAGE as PI_IMAGE


class ProbeParser(OutputParser):
    def feed(self, stream, line):
        return [("agent.output", {"stream": stream, "kind": "probe", "text": line[:300]})]

    def classify(self, exit_code):
        return None if exit_code == 0 else Failure("agent_error", f"probe exited with {exit_code}")

    def summary(self):
        return {}


class ProbeRuntime(AgentRuntime):
    name = "probe"
    default_image = PI_IMAGE

    def prepare(self, req):
        o = req.options
        return LaunchPlan(
            image=req.image or PI_IMAGE, args=list(o.get("args", ["120"])), files={"note.txt": "probe"},
            entrypoint=o.get("entrypoint", "/bin/sleep"), required_image_uid_label="multiagent.poc.agent_uid")

    def new_parser(self):
        return ProbeParser()


def register_probe():
    register(ProbeRuntime())
