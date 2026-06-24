"""HTTP API in front of the agent.

Endpoints:

  POST /api/chat                      send a customer message, get the reply + UI events
  POST /api/transfer/decision         approve or decline a pending transfer (deterministic)
  GET  /api/accounts                  demo account balances for the UI panel
  GET  /api/transactions              recent ledger entries (money workflows moved)
  GET  /health

The decision endpoint is the guardrail in action: approving a transfer is a
direct, deterministic call into the workflow triggered by the human tapping a
button. It does not pass through the language model, so the model cannot decide
to move money on its own.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent import agent
from agent.skills import Session
from banking import service as bank
from skills.money_transfer import client as transfer

SESSIONS: dict[str, Session] = {}

ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
).split(",")


@asynccontextmanager
async def lifespan(app: FastAPI):
    bank.seed()  # ensure the demo bank exists (idempotent)
    yield


app = FastAPI(title="Durable money assistant", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None


class ChatOut(BaseModel):
    session_id: str
    text: str
    events: list[dict[str, Any]] = []


class DecisionIn(BaseModel):
    session_id: str
    reference_id: str
    approved: bool


class DecisionOut(BaseModel):
    text: str
    result: dict[str, Any]


def _session(session_id: str | None) -> tuple[str, Session]:
    if session_id and session_id in SESSIONS:
        return session_id, SESSIONS[session_id]
    new_id = session_id or uuid.uuid4().hex
    session = Session()
    SESSIONS[new_id] = session
    return new_id, session


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/accounts")
async def accounts() -> dict[str, list[dict[str, Any]]]:
    ids = ["85-150", "43-812", "22-019", "55-200"]
    out = []
    for account_id in ids:
        info = transfer.lookup_account(account_id)
        if info.get("exists"):
            out.append(info)
    return {"accounts": out}


@app.get("/api/transactions")
async def transactions() -> dict[str, list[dict[str, Any]]]:
    """The bank ledger: money workflows actually moved, newest first."""
    return {"transactions": transfer.list_transactions()}


@app.post("/api/chat", response_model=ChatOut)
async def chat(body: ChatIn) -> ChatOut:
    session_id, session = _session(body.session_id)
    turn = await agent.run_turn(session, body.message)
    return ChatOut(session_id=session_id, text=turn.text, events=turn.events)


@app.post("/api/transfer/decision", response_model=DecisionOut)
async def decide(body: DecisionIn) -> DecisionOut:
    session = SESSIONS.get(body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session.")

    outcome = await transfer.submit_decision(body.reference_id, body.approved)
    result = outcome["result"]
    text = agent.outcome_message(result)

    # Record the outcome in the conversation so follow-up questions have context.
    session.messages.append({"role": "assistant", "content": text})
    if session.pending_reference_id == body.reference_id:
        session.pending_reference_id = None

    return DecisionOut(text=text, result=result)
