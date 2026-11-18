#!/bin/bash
# Prepares Pi's agent dir from task-scoped read-only inputs, then execs Pi with the args given by the orchestrator.
#   /run/task/models.json   provider/model definition (no secrets)
#   /run/secrets/<NAME>     optional credentials, one file per environment variable name
#   /run/llm/llm.sock       optional model-API relay socket (host side filters routes/models)
set -u

mkdir -p "$PI_CODING_AGENT_DIR"
if [ -f /run/task/models.json ]; then
  cp /run/task/models.json "$PI_CODING_AGENT_DIR/models.json"
fi

# Secrets become environment variables of the Pi process only (never of the container config / docker inspect).
if [ -d /run/secrets ]; then
  for f in /run/secrets/*; do
    [ -f "$f" ] || continue
    export "$(basename "$f")=$(cat "$f")"
  done
fi

if [ -S "${LLM_SOCKET:-/run/llm/llm.sock}" ]; then
  node /opt/runtime/llm-relay.js &
  for _ in $(seq 1 50); do
    (exec 3<>"/dev/tcp/127.0.0.1/${LLM_PORT:-11434}") 2>/dev/null && break
    sleep 0.1
  done
fi

if [ -f /run/task/prompt.txt ]; then
  # The prompt is read from a file so it never appears in `docker inspect` / process listings of the host.
  exec pi "$@" -- "$(cat /run/task/prompt.txt)"
fi
exec pi "$@"
