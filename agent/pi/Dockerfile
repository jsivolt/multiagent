# Phase 2 runtime image: the real Pi coding agent (pinned by package-lock.json) + tooling for coding tasks.
# Build via `python -m orchestrator build --runtime pi` (passes the host uid so bind-mounted workspaces stay writable).
FROM node:22-bookworm-slim

ARG AGENT_UID=1000
ARG AGENT_GID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates git ripgrep fd-find python3 python3-pytest \
 && rm -rf /var/lib/apt/lists/* \
 && ln -s /usr/bin/fdfind /usr/local/bin/fd \
 && (userdel -r node 2>/dev/null || true) \
 && groupadd -g ${AGENT_GID} agent \
 && useradd -u ${AGENT_UID} -g ${AGENT_GID} -M -d /home/agent -s /bin/bash agent \
 && mkdir -p /home/agent /workspace && chown ${AGENT_UID}:${AGENT_GID} /home/agent /workspace

WORKDIR /opt/pi
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev \
 && ln -s /opt/pi/node_modules/.bin/pi /usr/local/bin/pi \
 && pi --version

COPY entrypoint.sh llm-relay.js /opt/runtime/
RUN chmod 0755 /opt/runtime/entrypoint.sh

# Offline: nothing may phone home; all model traffic goes through the in-container relay.
ENV HOME=/home/agent \
    PI_CODING_AGENT_DIR=/home/agent/.pi/agent \
    PI_OFFLINE=1 \
    PI_SKIP_VERSION_CHECK=1 \
    PI_TELEMETRY=0 \
    CI=1

LABEL multiagent.poc.runtime="pi" multiagent.poc.agent_uid="${AGENT_UID}"
USER ${AGENT_UID}:${AGENT_GID}
WORKDIR /workspace
ENTRYPOINT ["/opt/runtime/entrypoint.sh"]
