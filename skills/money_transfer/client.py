"""The bridge between the agent and the durable workflow.

This is the thin agentic surface the determinism memo describes: the agent never
implements financial logic, it only

  * reads balances and quotes (cheap, read-only, straight from the bank), and
  * starts a transfer workflow and relays a human's decision into it.

Everything that moves money lives behind ``initiate_transfer`` /
``submit_decision`` and runs inside Temporal.
"""

from __future__ import annotations

import asyncio
import os

from temporalio.client import Client, WorkflowHandle, WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError

from banking import service as bank
from skills.money_transfer.shared import (
    ApprovalDecision,
    TASK_QUEUE,
    TransferPlan,
    TransferRequest,
    TransferResult,
)
from skills.money_transfer.workflow import MoneyTransferWorkflow

TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")

# Optional: when set, `initiate_transfer` reaches MoneyTransferWorkflow through a
# Nexus operation (via a thin caller workflow) instead of starting it directly.
# Either way the backing workflow id is the same, so approval and queries below
# are unchanged. Default is the direct path.
USE_NEXUS = os.environ.get("USE_NEXUS", "false").strip().lower() in {"1", "true", "yes"}

# Statuses at which initiation is done handing control back to the human/agent.
_SETTLED_AFTER_START = {
    "awaiting_approval",
    "invalid",
    "declined",
    "expired",
    "failed",
    "completed",
}

_client: Client | None = None
_client_lock = asyncio.Lock()


async def get_client() -> Client:
    """Connect once and reuse. The Pydantic converter handles our models."""
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = await Client.connect(
                    TEMPORAL_ADDRESS,
                    namespace=TEMPORAL_NAMESPACE,
                    data_converter=pydantic_data_converter,
                )
    return _client


def workflow_id_for(reference_id: str) -> str:
    return f"transfer-{reference_id}"


def nexus_workflow_id_for(reference_id: str) -> str:
    """Id of the Nexus *caller* workflow (distinct from the backing transfer).

    Only used on the ``USE_NEXUS`` path. The caller's sole job is to invoke the
    Nexus operation, which starts the backing ``transfer-{reference_id}`` workflow.
    """
    return f"nexus-transfer-{reference_id}"


# ---- Read-only helpers (no money moves) ------------------------------------


def lookup_account(account_id: str) -> dict:
    account = bank.get_account(account_id)
    if account is None:
        return {"account_id": account_id, "exists": False}
    return {
        "account_id": account.account_id,
        "exists": True,
        "name": account.name,
        "kind": account.kind,
        "status": account.status,
        "balance_dollars": round(account.balance_cents / 100, 2),
    }


def list_transactions(limit: int = 25) -> list[dict]:
    """Recent transactions from the bank ledger, newest first. Read-only.

    Powers the UI's activity feed. Each entry corresponds to money a workflow
    Activity actually moved, with amounts in dollars for display.
    """
    return [
        {
            "txn_id": t.txn_id,
            "account_id": t.account_id,
            "kind": t.kind,
            "amount_dollars": round(t.amount_cents / 100, 2),
            "reference_id": t.reference_id,
            "created_at": t.created_at,
        }
        for t in bank.list_transactions(limit=limit)
    ]


def quote_transfer(source_account: str, target_account: str, amount_cents: int) -> dict:
    """Validate and price a transfer without moving anything."""
    problems = bank.validate_transfer(source_account, target_account, amount_cents)
    fee = bank.calculate_fee(amount_cents)
    return {
        "valid": len(problems) == 0,
        "problems": problems,
        "amount_dollars": round(fee.amount_cents / 100, 2),
        "fee_dollars": round(fee.fee_cents / 100, 2),
        "total_debit_dollars": round(fee.total_debit_cents / 100, 2),
    }


# ---- The durable money path ------------------------------------------------


async def initiate_transfer(request: TransferRequest) -> dict:
    """Start the workflow and wait until it has a plan ready (or has settled).

    Returns the workflow id, the computed plan, and the current status. No money
    moves here: a valid transfer parks in ``awaiting_approval`` until a human
    decides.
    """
    client = await get_client()
    workflow_id = workflow_id_for(request.reference_id)

    if USE_NEXUS:
        # Start the backing workflow through the Nexus operation. The caller
        # workflow's handler starts MoneyTransferWorkflow at `workflow_id`, so we
        # then attach to that backing workflow for the plan/approval/queries below
        # exactly as the direct path does.
        from skills.money_transfer.nexus_impl import StartTransferWorkflow

        try:
            await client.start_workflow(
                StartTransferWorkflow.run,
                request,
                id=nexus_workflow_id_for(request.reference_id),
                task_queue=TASK_QUEUE,
            )
        except RPCError:
            # Same reference already in flight; the backing workflow exists (or
            # soon will). Fall through and attach to it.
            pass
        handle = client.get_workflow_handle(workflow_id)
    else:
        try:
            handle = await client.start_workflow(
                MoneyTransferWorkflow.run,
                request,
                id=workflow_id,
                task_queue=TASK_QUEUE,
            )
        except RPCError:
            # Same reference started already; attach to the running execution.
            handle = client.get_workflow_handle(workflow_id)

    status = await _await_plan_ready(handle)
    plan = await handle.query(MoneyTransferWorkflow.get_plan)
    result = await handle.query(MoneyTransferWorkflow.get_result)
    return {
        "workflow_id": workflow_id,
        "status": status,
        "plan": plan.model_dump() if plan else None,
        "result": result.model_dump() if result else None,
    }


async def submit_decision(
    reference_id: str, approved: bool, reason: str = ""
) -> dict:
    """Relay a human's approve/decline into the workflow and return the outcome."""
    client = await get_client()
    # Give the handle a result type so the Pydantic converter decodes the
    # workflow's result into a TransferResult; an untyped handle would hand back
    # a plain dict and ``.model_dump()`` below would fail.
    handle = client.get_workflow_handle(
        workflow_id_for(reference_id), result_type=TransferResult
    )
    try:
        await handle.execute_update(
            MoneyTransferWorkflow.submit_decision,
            ApprovalDecision(approved=approved, reason=reason),
        )
    except WorkflowUpdateFailedError:
        # The decision was rejected by the workflow's validator — the approval
        # window already expired, or a decision was already recorded. That is not
        # an error to surface as a 500; the workflow has (or will have) a real
        # terminal state, so fall through and report it. RPCError (e.g. Temporal
        # unreachable) is intentionally NOT caught: that is a transient failure
        # the caller should see and retry.
        pass
    result: TransferResult = await handle.result()
    return {"workflow_id": workflow_id_for(reference_id), "result": result.model_dump()}


async def get_state(reference_id: str) -> dict:
    client = await get_client()
    handle = client.get_workflow_handle(workflow_id_for(reference_id))
    status = await handle.query(MoneyTransferWorkflow.get_status)
    plan = await handle.query(MoneyTransferWorkflow.get_plan)
    result = await handle.query(MoneyTransferWorkflow.get_result)
    return {
        "workflow_id": workflow_id_for(reference_id),
        "status": status,
        "plan": plan.model_dump() if plan else None,
        "result": result.model_dump() if result else None,
    }


async def _await_plan_ready(
    handle: WorkflowHandle, timeout_seconds: float = 10.0
) -> str:
    """Poll status until the plan is built (awaiting approval) or it has settled.

    Tolerates the backing workflow not existing yet: on the Nexus path the caller
    workflow is still starting it, so an early query can raise NOT_FOUND. We treat
    that as "not ready" and keep polling until the deadline.
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    status = "validating"
    while True:
        try:
            status = await handle.query(MoneyTransferWorkflow.get_status)
        except RPCError:
            status = "validating"
        else:
            if status in _SETTLED_AFTER_START:
                return status
        if asyncio.get_running_loop().time() > deadline:
            return status
        await asyncio.sleep(0.15)
