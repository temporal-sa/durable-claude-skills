"""Workflow tests covering every meaningful outcome.

Run with:  pytest skills/money_transfer/tests -q

These are pure workflow unit tests: the real banking activities are replaced by
in-memory async mocks registered under the same activity names, and the workflow
runs against Temporal's time-skipping test server. That keeps the tests fast and
deterministic (no SQLite, no real clock) while still exercising the actual plan,
approval-gate, and saga code in the workflow.

Two best practices are on display here:

* **Mock at the activity boundary.** The workflow refers to activities by name,
  so a mock decorated with ``@activity.defn(name="...")`` is a drop-in. We assert
  on the workflow's result and on which activities ran, not on bank balances.
* **Skip time instead of waiting.** With ``start_time_skipping`` the environment
  only fast-forwards while a workflow ``result()`` is awaited, so we can safely
  deliver an approval *before* awaiting the result, and use ``env.sleep`` to fire
  the 10-minute approval timeout instantly.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from skills.money_transfer import client as transfer
from skills.money_transfer.shared import (
    TASK_QUEUE,
    ApprovalDecision,
    LedgerInput,
    TransferPlan,
    TransferRequest,
)
from skills.money_transfer.workflow import APPROVAL_TIMEOUT, MoneyTransferWorkflow


def _request(**overrides) -> TransferRequest:
    base = dict(
        source_account="85-150",
        target_account="43-812",
        amount_cents=100_00,
        reference_id=uuid.uuid4().hex[:12],
    )
    base.update(overrides)
    return TransferRequest(**base)


def _valid_plan(request: TransferRequest) -> TransferPlan:
    return TransferPlan(
        source_account=request.source_account,
        target_account=request.target_account,
        amount_cents=request.amount_cents,
        fee_cents=0,
        total_debit_cents=request.amount_cents,
        source_balance_before_cents=5_000_00,
        valid=True,
        problems=[],
    )


def _invalid_plan(request: TransferRequest, problem: str) -> TransferPlan:
    return TransferPlan(
        source_account=request.source_account,
        target_account=request.target_account,
        amount_cents=request.amount_cents,
        fee_cents=0,
        total_debit_cents=request.amount_cents,
        source_balance_before_cents=5_000_00,
        valid=False,
        problems=[problem],
    )


@dataclass
class MockBank:
    """Async activity mocks that record every ledger call.

    Registered under the same names as the real activities, so the workflow
    treats them as the genuine article. ``deposit_error`` lets a test make the
    deposit fail the way a freeze would, which drives the saga's refund path.
    """

    plan: TransferPlan
    deposit_error: str | None = None
    calls: dict[str, list[LedgerInput]] = field(
        default_factory=lambda: {"withdraw": [], "deposit": [], "refund": []}
    )

    def activities(self) -> list:
        @activity.defn(name="build_transfer_plan")
        async def build_transfer_plan(request: TransferRequest) -> TransferPlan:
            return self.plan

        @activity.defn(name="withdraw")
        async def withdraw(input: LedgerInput) -> str:
            self.calls["withdraw"].append(input)
            return f"withdraw-{input.reference_id}"

        @activity.defn(name="deposit")
        async def deposit(input: LedgerInput) -> str:
            self.calls["deposit"].append(input)
            if self.deposit_error is not None:
                # Mirror how a frozen destination surfaces: a non-retryable
                # bank error that the workflow's retry policy will not retry.
                raise ApplicationError(
                    self.deposit_error,
                    type="AccountFrozenError",
                    non_retryable=True,
                )
            return f"deposit-{input.reference_id}"

        @activity.defn(name="refund")
        async def refund(input: LedgerInput) -> str | None:
            self.calls["refund"].append(input)
            return f"refund-{input.reference_id}"

        return [build_transfer_plan, withdraw, deposit, refund]


async def _start(env_client: Client, request: TransferRequest, task_queue: str):
    return await env_client.start_workflow(
        MoneyTransferWorkflow.run,
        request,
        id=f"transfer-{request.reference_id}",
        task_queue=task_queue,
    )


async def _await_status(handle, expected: str) -> None:
    """Poll the status query until the workflow reaches ``expected``.

    ``start_workflow`` returns before the first workflow task finishes, so the
    status can still be ``validating`` for a beat. Polling the query (which does
    not skip time) lets the planning activity land before we assert.
    """
    for _ in range(100):
        if await handle.query(MoneyTransferWorkflow.get_status) == expected:
            return
        await asyncio.sleep(0.05)
    status = await handle.query(MoneyTransferWorkflow.get_status)
    raise AssertionError(f"expected status {expected!r}, still {status!r}")


@pytest.mark.asyncio
async def test_happy_path_moves_money_after_approval():
    request = _request(amount_cents=100_00)
    bank = MockBank(plan=_valid_plan(request))
    task_queue = str(uuid.uuid4())

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[MoneyTransferWorkflow],
            activities=bank.activities(),
        ):
            handle = await _start(env.client, request, task_queue)

            await _await_status(handle, "awaiting_approval")
            # No money has moved while awaiting approval.
            assert bank.calls["withdraw"] == []
            assert bank.calls["deposit"] == []

            # Deliver the approval before awaiting the result, so the
            # time-skipping server never fast-forwards past it.
            await handle.execute_update(
                MoneyTransferWorkflow.submit_decision,
                ApprovalDecision(approved=True),
            )
            result = await handle.result()

    assert result.status == "completed"
    assert result.withdraw_txn_id is not None
    assert result.deposit_txn_id is not None
    # The source was debited the full amount and the target credited the amount.
    assert bank.calls["withdraw"][0].amount_cents == 100_00
    assert bank.calls["deposit"][0].amount_cents == 100_00
    assert bank.calls["refund"] == []  # nothing rolled back


@pytest.mark.asyncio
async def test_decline_moves_no_money():
    request = _request()
    bank = MockBank(plan=_valid_plan(request))
    task_queue = str(uuid.uuid4())

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[MoneyTransferWorkflow],
            activities=bank.activities(),
        ):
            handle = await _start(env.client, request, task_queue)
            await _await_status(handle, "awaiting_approval")
            await handle.execute_update(
                MoneyTransferWorkflow.submit_decision,
                ApprovalDecision(approved=False, reason="changed my mind"),
            )
            result = await handle.result()

    assert result.status == "declined"
    assert bank.calls["withdraw"] == []
    assert bank.calls["deposit"] == []
    assert bank.calls["refund"] == []


@pytest.mark.asyncio
async def test_invalid_plan_never_waits_for_approval():
    request = _request(target_account="99-999")
    bank = MockBank(plan=_invalid_plan(request, "target account 99-999 does not exist"))
    task_queue = str(uuid.uuid4())

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[MoneyTransferWorkflow],
            activities=bank.activities(),
        ):
            result = await env.client.execute_workflow(
                MoneyTransferWorkflow.run,
                request,
                id=f"transfer-{request.reference_id}",
                task_queue=task_queue,
            )

    assert result.status == "invalid"
    assert any("99-999" in p for p in result.plan.problems)
    # An invalid plan never reaches the ledger.
    assert bank.calls["withdraw"] == []


@pytest.mark.asyncio
async def test_expired_when_no_decision_arrives():
    """If no decision lands within the approval window, the transfer expires."""
    request = _request()
    bank = MockBank(plan=_valid_plan(request))
    task_queue = str(uuid.uuid4())

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[MoneyTransferWorkflow],
            activities=bank.activities(),
        ):
            handle = await _start(env.client, request, task_queue)
            await _await_status(handle, "awaiting_approval")

            # Jump past the approval window instead of waiting ten real minutes.
            await env.sleep(APPROVAL_TIMEOUT + APPROVAL_TIMEOUT)
            result = await handle.result()

    assert result.status == "expired"
    assert bank.calls["withdraw"] == []
    assert bank.calls["deposit"] == []


@pytest.mark.asyncio
async def test_deposit_failure_refunds_the_source():
    """If the destination freezes after planning, the debit is rolled back."""
    request = _request(amount_cents=100_00)
    bank = MockBank(plan=_valid_plan(request), deposit_error="destination account is frozen")
    task_queue = str(uuid.uuid4())

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[MoneyTransferWorkflow],
            activities=bank.activities(),
        ):
            handle = await _start(env.client, request, task_queue)
            await _await_status(handle, "awaiting_approval")

            await handle.execute_update(
                MoneyTransferWorkflow.submit_decision,
                ApprovalDecision(approved=True),
            )
            result = await handle.result()

    assert result.status == "failed"
    assert result.refund_txn_id is not None
    # The withdraw committed, the deposit failed, and the saga refunded the debit.
    assert bank.calls["withdraw"][0].amount_cents == 100_00
    assert len(bank.calls["deposit"]) == 1
    assert bank.calls["refund"][0].amount_cents == 100_00  # full debit returned


@pytest.mark.asyncio
async def test_client_bridge_initiate_then_decide(monkeypatch):
    """Drive the agent-facing client bridge end to end.

    The workflow tests above talk to Temporal directly. This one goes through
    skills/money_transfer/client.py — the surface the agent API actually calls —
    so it covers ``initiate_transfer`` and ``submit_decision`` returning JSON-able
    dicts. It is a regression test for ``submit_decision`` using an untyped handle,
    which made ``handle.result()`` hand back a plain dict and crash on
    ``.model_dump()``.
    """
    request = _request(amount_cents=100_00)
    bank = MockBank(plan=_valid_plan(request))

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        # Point the client module's cached connection at the test environment.
        monkeypatch.setattr(transfer, "_client", env.client)
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,  # the bridge starts workflows on this queue
            workflows=[MoneyTransferWorkflow],
            activities=bank.activities(),
        ):
            started = await transfer.initiate_transfer(request)
            assert started["status"] == "awaiting_approval"
            assert started["plan"]["valid"] is True

            outcome = await transfer.submit_decision(request.reference_id, approved=True)

    # The result must be a plain dict (JSON-able for the API), not a model.
    assert isinstance(outcome["result"], dict)
    assert outcome["result"]["status"] == "completed"
    assert outcome["result"]["deposit_txn_id"] is not None
