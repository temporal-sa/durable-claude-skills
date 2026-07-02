# syntax=docker/dockerfile:1
# All-in-one image: builds the React/Vite UI, then runs the Temporal worker, the
# FastAPI agent API, and nginx (which serves the UI and reverse-proxies /api to
# the API) together in ONE container. Because the worker and API share the same
# filesystem here, they also share the simulated bank's SQLite ledger — so the
# activity feed reflects money the worker actually moved.
#
# Handy for a single-command demo. For anything beyond that, prefer the split
# app.Dockerfile + web.Dockerfile so the UI and API scale independently.
#
# Build from the repo root (context must be the repo root):
#   docker build -f docker/combined.Dockerfile -t durable-money-assistant .
#
# Run (Temporal + Anthropic key are supplied at runtime):
#   docker run --rm -p 8080:8080 \
#     -e ANTHROPIC_API_KEY=sk-ant-... \
#     -e TEMPORAL_ADDRESS=host.docker.internal:7233 \
#     durable-money-assistant
# Then open http://localhost:8080
#
# Notes:
#   * TEMPORAL_ADDRESS must point at a reachable Temporal frontend. Inside a
#     container "localhost" is the container itself, so to reach a dev server on
#     the host use host.docker.internal:7233 (Docker Desktop) or --network=host
#     with localhost:7233 (Linux).
#   * The browser calls /api on the page's own origin; nginx proxies it to the
#     API in this same container, so no CORS config is needed.

# ---- Stage 1: build the static UI -----------------------------------------
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---- Stage 2: Python app + nginx ------------------------------------------
FROM python:3.12-slim

# nginx serves the UI and proxies /api; tini is a tiny init for clean signal
# handling and zombie reaping since we run several processes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx tini \
    && rm -rf /var/lib/apt/lists/*

# uv for fast, lockfile-faithful installs straight from uv.lock. Pin for repro.
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /bin/uv

WORKDIR /app

# Python deps first, for layer caching. --no-install-project: we run from source.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Application source (Python).
COPY agent/ ./agent/
COPY banking/ ./banking/
COPY skills/ ./skills/
COPY worker.py ./

# Built UI from stage 1, plus an nginx site: serve the SPA on 8080, proxy /api
# and /health to the local API. $uri / $host are nginx vars (single-quoted so
# the shell leaves them alone).
COPY --from=web /web/dist /usr/share/nginx/html
RUN printf '%s\n' \
    'server {' \
    '    listen 8080;' \
    '    root /usr/share/nginx/html;' \
    '    location /api/ { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; proxy_set_header X-Forwarded-For $remote_addr; }' \
    '    location /health { proxy_pass http://127.0.0.1:8000; }' \
    '    location / { try_files $uri /index.html; }' \
    '}' \
    > /etc/nginx/conf.d/default.conf \
    && rm -f /etc/nginx/sites-enabled/default

ENV PATH="/app/.venv/bin:$PATH" \
    BANK_DB_PATH=/data/banking.db
RUN mkdir -p /data

EXPOSE 8080

# One entry point launches all three: worker and API in the background, nginx in
# the foreground (exec, so it receives signals; tini forwards them and reaps).
# The API binds to localhost only — reachable via the nginx /api proxy, not
# directly. The ledger is ephemeral and re-seeded on boot, so restarts are fine.
# NOTE: a worker or API crash does not stop the container; acceptable for a demo.
ENTRYPOINT ["tini", "--"]
CMD ["sh", "-c", "python worker.py & python -m uvicorn agent.server:app --host 127.0.0.1 --port 8000 & exec nginx -g 'daemon off;'"]
