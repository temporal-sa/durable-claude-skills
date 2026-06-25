# syntax=docker/dockerfile:1
# Combined worker + agent API. Both processes run in this one container so they
# share the simulated bank's SQLite ledger on the local filesystem — the demo's
# activity feed only reflects money the worker actually moved, which requires
# the API and worker to see the same banking.db.
FROM python:3.12-slim

# uv for fast, lockfile-faithful installs straight from uv.lock — no
# requirements.txt needed. Pin the uv version for reproducible builds.
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /bin/uv

WORKDIR /app

# Dependencies first, for layer caching. --no-install-project: we run from the
# source tree (below), so only the third-party deps need to be in the venv.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Application source (Python only; the web UI is a separate image).
COPY agent/ ./agent/
COPY banking/ ./banking/
COPY skills/ ./skills/
COPY worker.py ./

ENV PATH="/app/.venv/bin:$PATH" \
    BANK_DB_PATH=/data/banking.db
RUN mkdir -p /data

EXPOSE 8000

# Worker in the background, uvicorn in the foreground via exec so it receives
# SIGTERM on shutdown. The ledger is ephemeral and re-seeded on boot, so a
# restart is fine for a demo. NOTE: a worker crash does not exit the container
# (uvicorn stays up); acceptable for a demo, not for production.
CMD ["sh", "-c", "python worker.py & exec python -m uvicorn agent.server:app --host 0.0.0.0 --port 8000"]
