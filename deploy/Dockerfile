# ShutterStorm images. Build from the repo root (the compose file does this):
#   docker compose --env-file shutterstorm.env -f deploy/docker-compose.yml build
#
# Two targets, on purpose different:
#   agents      - the AI agents service. Python + requests only: no Docker CLI, runs as a
#                 non-root user, read-only filesystem in compose. It never gets the Docker socket.
#   controller  - workflow, probes, faults, dashboard and the guard. Needs the Docker CLI and
#                 the compose plugin (dependency discovery runs `docker compose config`).

FROM python:3.12-slim AS agents
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shutterstorm ./shutterstorm
RUN useradd --system --uid 10001 agent \
    && mkdir -p /app/.shutterstorm/llm_cache \
    && chown -R agent /app/.shutterstorm
USER agent
ENV SHUTTERSTORM_HOME=/app PYTHONUNBUFFERED=1
EXPOSE 8780
CMD ["python", "-m", "shutterstorm.agents_service", "--host", "0.0.0.0", "--port", "8780"]

FROM docker:27.3.1-cli AS controller
RUN apk add --no-cache python3 py3-requests tzdata
WORKDIR /app
COPY shutterstorm ./shutterstorm
COPY docs ./docs
ENV SHUTTERSTORM_HOME=/app SHUTTERSTORM_IN_CONTAINER=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "-m", "shutterstorm"]
CMD ["doctor"]
