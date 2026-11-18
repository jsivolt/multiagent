FROM python:3.12-alpine

# Non-root runtime user; code is root-owned and read-only for that user.
RUN addgroup -g 10001 agent && adduser -D -H -u 10001 -G agent agent

COPY agent.py /app/agent.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKSPACE=/workspace

USER 10001:10001
WORKDIR /app
ENTRYPOINT ["python", "/app/agent.py"]
