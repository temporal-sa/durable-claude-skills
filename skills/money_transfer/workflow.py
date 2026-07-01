"""The money-transfer workflow: the deterministic backbone of the skill.

The shape of a transfer is:

    1. build a validated, costed plan          (activity)
    2. wait for an explicit human decision      (durable gate; times out)
    3. if approved, run withdraw -> deposit      (saga; refunds on failure)

Step 2 is the important guardrail. Money cannot move until a real approval token
arrives, and that rule is enforced here in deterministic code, not by the
agent's good behavior. Even a confused or jailbroken agent can only *start* this
workflow and *relay* a decision; it cannot skip the gate.

The whole function is the graph. A reviewer reads one method to see every step,
its ordering, and its recovery, and CI can replay it.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Awaitable, Callable

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, is_cancelled_exception

with workflow.unsafe.imports_passed_through():
    from skills.money_transfer.activities import (
        build_transfer_plan,
        deposit,
        refund,
        withdraw,
    )
    from skills.money_transfer.shared import (
        ApprovalDecision,
        LedgerInput,
        TransferPlan,
        TransferRequest,
        TransferResult,
        TransferStatus,
    )

# Bank validation failures are deterministic facts about the request: retrying
# will not change the outcome, so fail fast and let the workflow handle it.
_NON_RETRYABLE_BANK_ERRORS = [
    "InvalidAccountError",
    "InsufficientFundsError",
    "AccountFrozenError",
    "AccountClosedError",
    "TransferLimitError",
]

_BANK_RETRY_POLICY = RetryPolicy(
    non_retryable_error_types=_NON_RETRYABLE_BANK_ERRORS,
)

# The deposit retries on a constant 5-second interval (rather than the default
# exponential backoff) so the demo's simulated deposit failures retry visibly —
# a 5-second wait between each attempt — before the deposit finally succeeds.
_DEPOSIT_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=1.0,
    maximum_interval=timedelta(seconds=5),
    non_retryable_error_types=_NON_RETRYABLE_BANK_ERRORS,
)

#: How long to hold a transfer waiting for a human decision before expiring it.
APPROVAL_TIMEOUT = timedelta(minutes=10)

_LEDGER_TIMEOUT = timedelta(seconds=10)


def _failure_detail(err: ActivityError) -> str:
    """Plain-language explanation for a failed (compensated) transfer.

    Special-cases a closed destination so the customer learns *why* the deposit
    failed and that their money was returned; otherwise a generic refund message.
    """
    cause = err.cause
    if isinstance(cause, ApplicationError) and cause.type == "AccountClosedError":
        return (
            "We couldn't deposit the funds because the destination account is "
            "closed. The withdrawal was rolled back, so your balance has been "
            "fully refunded."
        )
    return (
        "The transfer could not be completed and any debit was returned to the "
        "source account."
    )


@workflow.defn
class MoneyTransferWorkflow:
    def __init__(self) -> None:
        self._status: TransferStatus = "validating"
        self._plan: TransferPlan | None = None
        self._decision: ApprovalDecision | None = None
        self._result: TransferResult | None = None

    # -- Queries: read-only views the agent and UI poll ----------------------

    @workflow.query
    def get_status(self) -> str:
        return self._status

    @workflow.query
    def get_plan(self) -> TransferPlan | None:
        return self._plan

    @workflow.query
    def get_result(self) -> TransferResult | None:
        return self._result

    # -- Update: the human decision, relayed by the confirmation step --------

    @workflow.update
    async def submit_decision(self, decision: ApprovalDecision) -> str:
        self._decision = decision
        return "recorded"

    @submit_decision.validator
    def _validate_decision(self, decision: ApprovalDecision) -> None:
        # Reject anything that would let a decision land at the wrong time. The
        # validator is read-only and runs before the update is admitted.
        if self._status != "awaiting_approval":
            raise ValueError(
                f"This transfer is '{self._status}', not awaiting approval."
            )
        if self._decision is not None:
            raise ValueError("A decision has already been recorded for this transfer.")

    # -- The workflow itself -------------------------------------------------

    @workflow.run
    async def run(self, request: TransferRequest) -> TransferResult:
        # Step 1: plan and validate.
        self._status = "validating"
        plan = await workflow.execute_activity(
            build_transfer_plan,
            request,
            start_to_close_timeout=_LEDGER_TIMEOUT,
            retry_policy=_BANK_RETRY_POLICY,
        )
        self._plan = plan

        if not plan.valid:
            self._status = "invalid"
            self._result = TransferResult(
                status="invalid",
                reference_id=request.reference_id,
                plan=plan,
                detail="; ".join(plan.problems),
            )
            return self._result

        # Step 2: wait for an explicit human decision.
        self._status = "awaiting_approval"
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None, timeout=APPROVAL_TIMEOUT
            )
        except asyncio.TimeoutError:
            self._status = "expired"
            self._result = TransferResult(
                status="expired",
                reference_id=request.reference_id,
                plan=plan,
                detail="No approval was received before the request expired.",
            )
            return self._result

        decision = self._decision
        assert decision is not None
        if not decision.approved:
            self._status = "declined"
            self._result = TransferResult(
                status="declined",
                reference_id=request.reference_id,
                plan=plan,
                detail=decision.reason or "Declined by the customer.",
            )
            return self._result

        # Step 3: execute the transfer as a saga.
        return await self._execute(request, plan)

    async def _execute(
        self, request: TransferRequest, plan: TransferPlan
    ) -> TransferResult:
        self._status = "executing"
        compensations: list[Callable[[], Awaitable[object]]] = []

        try:
            # Register the refund *before* withdrawing. If the withdraw commits
            # but the activity is then lost (timeout, crash), the effect still
            # happened, so the compensation must already be on the stack. The
            # refund activity is a no-op when nothing was actually withdrawn.
            compensations.append(
                lambda: workflow.execute_activity(
                    refund,
                    LedgerInput(
                        # Return the full debit (amount + fee) so a failed
                        # transfer never costs the customer anything.
                        account_id=request.source_account,
                        amount_cents=plan.total_debit_cents,
                        reference_id=request.reference_id,
                        idempotency_key=f"refund:{request.reference_id}",
                    ),
                    start_to_close_timeout=_LEDGER_TIMEOUT,
                )
            )
            withdraw_txn = await workflow.execute_activity(
                withdraw,
                LedgerInput(
                    account_id=request.source_account,
                    amount_cents=plan.total_debit_cents,
                    reference_id=request.reference_id,
                    idempotency_key=f"withdraw:{request.reference_id}",
                ),
                start_to_close_timeout=_LEDGER_TIMEOUT,
                retry_policy=_BANK_RETRY_POLICY,
            )

            deposit_txn = await workflow.execute_activity(
                deposit,
                LedgerInput(
                    account_id=request.target_account,
                    amount_cents=plan.amount_cents,
                    reference_id=request.reference_id,
                    idempotency_key=f"deposit:{request.reference_id}",
                ),
                start_to_close_timeout=_LEDGER_TIMEOUT,
                retry_policy=_DEPOSIT_RETRY_POLICY,
            )

            self._status = "completed"
            self._result = TransferResult(
                status="completed",
                reference_id=request.reference_id,
                plan=plan,
                withdraw_txn_id=withdraw_txn,
                deposit_txn_id=deposit_txn,
                detail="Transfer completed.",
            )
            return self._result

        except ActivityError as err:
            if is_cancelled_exception(err):
                raise
            workflow.logger.error("Transfer failed, compensating: %s", err)
            self._status = "compensating"

            refund_txn: str | None = None

            async def run_compensations() -> None:
                nonlocal refund_txn
                for compensate in reversed(compensations):
                    try:
                        refund_txn = await compensate()  # type: ignore[assignment]
                    except Exception as comp_err:  # noqa: BLE001
                        workflow.logger.error("Compensation failed: %s", comp_err)

            # Shield so compensations still run if the workflow is cancelled.
            await asyncio.shield(asyncio.ensure_future(run_compensations()))

            self._status = "failed"
            self._result = TransferResult(
                status="failed",
                reference_id=request.reference_id,
                plan=plan,
                refund_txn_id=refund_txn,
                detail=_failure_detail(err),
            )
            return self._result
