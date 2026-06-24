# Durable money assistant: Claude + Agent Skills + Temporal

A small but complete demo of one idea: **the agent runs the conversation;
deterministic code moves the money.**

A customer chats with a Claude-powered assistant about sending money. The
assistant can look up balances and price a transfer, but it can't move a cent on
its own. To actually send money, it *starts* a Temporal workflow that builds a
validated plan, **pauses for the customer's explicit approval**, and only then
runs `withdraw → deposit`, automatically refunding the debit if anything fails.
The approval gate and the refund live in workflow code, so they hold no matter
what the model says or does.

## How it works

Three pieces, with a bright line between talking and moving money:

- **The agent** (`agent/`) is an ordinary Claude tool-use loop. Its only
  money-related tools are read-only (`lookup_account`, `get_transfer_quote`) plus
  one `initiate_transfer` that *starts* the workflow. It can't finish a transfer;
  that needs a human approval the model has no way to issue.
- **The skill** (`skills/money_transfer/`) is what the agent points at: a
  plain-English contract (`SKILL.md`, loaded into the system prompt) paired with
  the deterministic `workflow.py` it describes. The workflow is the entire
  transfer in one method (plan → approval gate → a `withdraw → deposit` saga that
  refunds on failure), so it reads top to bottom, unit-tests, and replays in CI.
- **The bank** (`banking/`) is a simulated core-banking system in a single SQLite
  file, shared by the worker and the API so both see the same balances. Money
  moves only through the workflow's activities; everything they write is the
  record the UI's balances and activity feed read back.

```
  Browser (React + TS, Temporal-styled)
        │  POST /api/chat            POST /api/transfer/decision
        ▼                                   │ (deterministic: a human tap,
  FastAPI + Claude agent  ◀─────────────────┘  relayed straight to the workflow)
        │  read-only: lookup / quote  ──────────────►  bank (SQLite)
        │  initiate_transfer ─────────►  Temporal
        ▼
  Temporal worker  ──►  MoneyTransferWorkflow
                          1. build & validate plan        (activity)
                          2. wait for explicit approval    (durable gate, times out)
                          3. withdraw → deposit            (saga; refunds on failure)
                                   └► bank (SQLite)
```

The approval is the guardrail, and it lives in code rather than the prompt. A
valid transfer parks in `awaiting_approval` until the customer acts. Tapping
**Approve** sends a Temporal Update straight into the running workflow (it never
passes back through the model), so a transfer can't complete without a real human
decision, a correct refusal can't be argued away mid-conversation, and if no one
approves within ten minutes the transfer simply expires. Nothing moves until that
tap.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (manages Python 3.10+ and the virtualenv for you)
- Node 18+
- The [Temporal CLI](https://docs.temporal.io/cli) (`temporal`), for the local dev server
- An Anthropic API key

## Setup

```bash
# from the repo root
uv sync                     # creates .venv and installs deps (incl. the dev group)

cp .env.example .env        # then put your ANTHROPIC_API_KEY in .env

cd web && npm install && cd ..
```

`uv sync` reads `pyproject.toml`/`uv.lock`, so there's no separate venv or
activation step; every Python command below runs through `uv run`, which uses
that environment. The `--env-file .env` flag loads your `ANTHROPIC_API_KEY` (and
anything else from [Configuration](#configuration)) into the process; you can
also `export` the vars yourself or use `direnv` instead.

## Run it (four terminals)

```bash
# 1. Temporal dev server (includes the Web UI at http://localhost:8233)
temporal server start-dev

# 2. The workflow worker
uv run --env-file .env python worker.py

# 3. The agent API (http://localhost:8000), needs ANTHROPIC_API_KEY
uv run --env-file .env uvicorn agent.server:app --reload

# 4. The chat UI (http://localhost:5173)
cd web && npm run dev
```

Start them in order: Temporal first (the worker and API connect to it), then the
worker and API, then the UI. The worker and API both seed the SQLite bank on
startup, so the demo accounts exist as soon as either is running.

Open http://localhost:5173 and try one of the suggested prompts. When you start a
transfer, a confirmation card appears with the amount, fee, and total. Approving
it relays your decision into the workflow; the card then shows the outcome and a
link to the run in the Temporal Web UI. The left rail's **Recent activity** feed
then updates with the ledger entries the workflow wrote, so it reflects only
money that actually moved.

## Configuration

All variables are optional except `ANTHROPIC_API_KEY`. Copy `.env.example` to
`.env` and edit; `uv run --env-file .env` loads it.

| Variable             | Used by      | Default                                       | What it does                            |
|----------------------|--------------|-----------------------------------------------|-----------------------------------------|
| `ANTHROPIC_API_KEY`  | agent API    | **required**                                  | Authenticates the agent's Claude calls  |
| `CLAUDE_MODEL`       | agent API    | `claude-sonnet-4-6`                           | The model the agent uses                |
| `TEMPORAL_ADDRESS`   | worker + API | `localhost:7233`                              | Temporal frontend address               |
| `TEMPORAL_NAMESPACE` | worker + API | `default`                                     | Temporal namespace                      |
| `USE_NEXUS`          | worker + API | `false`                                       | Start the transfer through a Nexus operation instead of directly (see below) |
| `BANK_DB_PATH`       | worker + API | `./banking.db`                                | SQLite file the worker and API share    |
| `ALLOWED_ORIGINS`    | agent API    | `http://localhost:5173,http://127.0.0.1:5173` | Comma-separated CORS origins            |
| `VITE_API_TARGET`    | web (dev)    | `http://localhost:8000`                       | Where Vite proxies `/api` in dev        |
| `VITE_API_BASE`      | web (build)  | `""` (same-origin)                            | API origin the browser calls; set when serving the UI against a remote API |

The worker and API must agree on `BANK_DB_PATH` (it's how they share balances),
so if you change it, set it for both; `--env-file .env` does that for you.

The two `VITE_*` vars are build/dev-time Vite variables (read via
`import.meta.env`), so set them in `web/.env` or the build environment, not via
`uv run --env-file .env`. For local dev you don't need either: Vite proxies
`/api` to the FastAPI server on the same origin.

## Optional: run the skill through a Nexus operation

By default the agent starts `MoneyTransferWorkflow` directly. Set `USE_NEXUS=true`
to instead reach it through a **Temporal Nexus** operation. This is entirely
optional and off by default; the direct path is unchanged.

### Why — what Nexus adds

Nexus adds a *third* contract to the skill without removing the others:

- `SKILL.md` — the model-facing contract (still loaded into the prompt, unchanged).
- `MoneyTransferService` (`skills/money_transfer/nexus_service.py`) — the typed
  service-to-service contract, the new piece. A caller depends only on this, not
  on the workflow or banking code, so the skill could live in another namespace,
  repository, or team.
- `MoneyTransferWorkflow` — the deterministic implementation (unchanged).

Because Nexus operations are invoked from a workflow (not from arbitrary code),
the agent's `initiate_transfer` starts a thin caller workflow
(`StartTransferWorkflow`, in `skills/money_transfer/nexus_impl.py`) that runs the
operation. The operation handler starts `MoneyTransferWorkflow` at the **same** id
(`transfer-<reference_id>`), so the approval Update, status/plan queries, the UI,
and the bank are all identical to the direct path — only the *start* mechanism
changes.

```
  Direct (default):   initiate_transfer ─────────────────────────►  transfer-<ref>  (MoneyTransferWorkflow)
  Nexus (USE_NEXUS):  initiate_transfer ─► nexus-transfer-<ref> ─► (Nexus endpoint) ─► transfer-<ref>
                                            (caller workflow)        start_transfer op
```

### Setup (what's needed to demonstrate it)

Nexus support ships with Temporal's local dev server, so no special build is
needed — just three things beyond the normal run: create an endpoint once, flip
the flag, and restart the worker and API so both pick it up.

1. **Start the dev server** (if it isn't already running):

   ```bash
   temporal server start-dev
   ```

2. **Create the Nexus endpoint — once per server.** It routes
   `MoneyTransferService` calls to the worker's task queue. Either run the helper
   (idempotent — safe to re-run, prints "already exists" if so):

   ```bash
   uv run --env-file .env python scripts/setup_nexus.py
   ```

   or do the equivalent with the CLI:

   ```bash
   temporal operator nexus endpoint create \
     --name money-transfer-endpoint \
     --target-namespace default \
     --target-task-queue money-transfer
   ```

3. **Turn on the flag.** In `.env`, set:

   ```bash
   USE_NEXUS=true
   ```

4. **Restart the worker and the API** so both read the flag. They must agree:
   the flag gates both the agent's start path *and* the worker's registration of
   the caller workflow and operation handler.

   ```bash
   # worker
   uv run --env-file .env python worker.py
   # api
   uv run --env-file .env uvicorn agent.server:app --reload
   ```

### Verify it's using Nexus

- The worker prints its mode on startup: look for
  `Worker started on task queue 'money-transfer' (direct + Nexus path).` If it
  says `(direct)`, the worker didn't see `USE_NEXUS=true` — re-check `.env` and
  that you passed `--env-file .env` to the worker.
- Run a transfer in the UI, then open the Web UI (http://localhost:8233). With
  Nexus on you'll see **two** workflows per transfer: the `nexus-transfer-<ref>`
  caller and the `transfer-<ref>` execution it started through the operation.
  With the flag off there's only `transfer-<ref>`.

### Notes and gotchas

- **The worker and API must agree on `USE_NEXUS`.** If the API has it on but the
  worker has it off, you'll get
  `Workflow class StartTransferWorkflow is not registered on this worker` —
  restart the worker with the flag set.
- **Order matters on first run:** the endpoint (step 2) must exist before a
  Nexus-path transfer runs, or the caller workflow can't resolve
  `money-transfer-endpoint`.
- **Turning it back off:** set `USE_NEXUS=false` (or remove the line) and restart
  the worker and API. The endpoint can stay; it's harmless when unused.
- **Demo simplification:** the handler, caller, and agent all use the `default`
  namespace and the `money-transfer` task queue, so the agent can still query and
  update the backing workflow directly. A true cross-namespace/cross-team split
  is the natural next step Nexus enables.

## What to try

The seeded accounts are chosen so every path is reachable:

| Account  | What it is            | Demonstrates                         |
|----------|-----------------------|--------------------------------------|
| `85-150` | Checking, $5,000      | a normal source account              |
| `43-812` | Savings, $1,200       | a normal destination                 |
| `22-019` | Checking, $250        | insufficient funds                   |
| `55-200` | Frozen, $9,000        | a frozen account (transfer rejected) |
| `99-999` | (does not exist)      | an unknown account                   |

- **Happy path:** "Send $250 from 85-150 to 43-812", then approve.
- **A fee:** "$1,500 from 85-150 to 43-812" (over $1,000, so a 0.5% fee applies).
- **Rejected before approval:** a transfer to `99-999` or `55-200` comes back
  invalid with a plain explanation; no approval is requested.
- **Decline:** start a transfer and decline it; nothing moves.
- **Compensation:** the saga's refund path is covered by the tests (freeze the
  destination after planning, approve, watch the debit roll back).

## Tests

```bash
uv run pytest                        # the workflow suite (Temporal time-skipping)
uv run python scripts/smoke_test.py  # the bank's money invariants (stdlib only)
```

`uv run pytest` runs the workflow suite against Temporal's **time-skipping** test
server with the banking activities replaced by in-memory async **mocks**: no
real bank, no real clock, so it's fast and deterministic. It covers the five
meaningful outcomes: completed after approval, declined, invalid plan (never
waits for approval), expired (no decision before the approval window closes), and
a failed execution that refunds the source. The mock-based design lets the
expiry test fire the 10-minute approval timeout instantly via `env.sleep(...)`.

`scripts/smoke_test.py` is dependency-free (standard library only) and checks the
bank's financial core directly (fees, validation, idempotency, and the
withdraw → deposit → refund saga sequence) without needing Temporal or a key.

## Project layout

```
banking/            simulated core bank (SQLite, idempotent ops, fee/limit logic)
skills/money_transfer/
  SKILL.md          the agent-facing contract (loaded into the system prompt)
  shared.py         Pydantic contracts + task queue name
  activities.py     plan / withdraw / deposit / refund
  workflow.py       MoneyTransferWorkflow (plan → approval gate → saga)
  client.py         the bridge the agent uses to drive Temporal
  nexus_service.py  optional Nexus service contract (USE_NEXUS)
  nexus_impl.py     optional Nexus handler + caller workflow (USE_NEXUS)
  tests/            workflow tests
agent/
  skills.py         loads SKILL.md, defines the tools, dispatches them
  agent.py          the Claude tool-use loop
  server.py         FastAPI: /api/chat, /api/transfer/decision, /api/accounts, /api/transactions
worker.py           the Temporal worker
scripts/            seed_bank.py (reset balances), smoke_test.py (bank invariants), setup_nexus.py (create the Nexus endpoint)
web/                React + TypeScript chat UI, styled to the Temporal brand
```

## Notes

- **Idempotency.** Every money-moving activity takes an idempotency key, so
  Temporal's automatic retries apply each effect exactly once. The refund is a
  no-op when nothing was actually withdrawn.
- **Brand.** The UI uses Temporal's palette (UV `#444CE7`, Space Black `#141414`,
  Off White `#F8FAFC`) and an original sparkle mark. For the official logo, see
  `web/public/BRAND.md` and https://temporal.io/brand.
- **Not for production.** Sessions are in-memory, the bank is a SQLite toy, and
  there is no auth. It exists to demonstrate the pattern.
```
