"""Typed contracts for the money-transfer skill.

Everything that crosses the agent -> workflow -> activity boundary is a Pydantic
model. The agent fills in a TransferRequest, the workflow returns a TransferPlan
then a TransferResult, and the activities exchange the small *Input models. Using
Pydantic means each boundary is validated at runtime and the JSON schema can be
generated rather than hand-written.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: All money-transfer workflows and activities run on this task queue.
TASK_QUEUE = "money-transfer"

#: Lifecycle of a transfer. The agent and UI render these directly.
TransferStatus = Literal[
    "validating",        # building and checking the plan
    "invalid",           # plan failed validation; no approval was sought, no money moved
    "awaiting_approval", # plan is ready and waiting for an explicit human decision
    "executing",         # approved; withdraw/deposit in progress
    "compensating",      # a step failed; running the refund
    "completed",         # money moved successfully
    "declined",          # a human declined; no money moved
    "expired",           # no decision arrived in time; no money moved
    "failed",            # execution failed and was compensated
]


class TransferRequest(BaseModel):
    """What the agent hands to the workflow to start a transfer."""

    source_account: str
    target_account: str
    amount_cents: int = Field(gt=0, description="Amount to send, in cents.")
    reference_id: str = Field(
        description="Idempotency key for the whole transfer; also the workflow id suffix."
    )
    note: str = Field(default="", max_length=140)


class TransferPlan(BaseModel):
    """The validated, costed plan a human approves or declines. No money has moved."""

    source_account: str
    target_account: str
    amount_cents: int
    fee_cents: int
    total_debit_cents: int
    source_balance_before_cents: int | None = None
    valid: bool
    problems: list[str] = Field(default_factory=list)


class TransferResult(BaseModel):
    """The terminal outcome of the workflow."""

    status: TransferStatus
    reference_id: str
    plan: TransferPlan
    withdraw_txn_id: str | None = None
    deposit_txn_id: str | None = None
    refund_txn_id: str | None = None
    detail: str = ""


class ApprovalDecision(BaseModel):
    """A human's decision, relayed into the workflow by the confirmation step."""

    approved: bool
    decided_by: str = "customer"
    reason: str = ""


# ---- Activity input models -------------------------------------------------


class LedgerInput(BaseModel):
    account_id: str
    amount_cents: int
    reference_id: str
    idempotency_key: str
