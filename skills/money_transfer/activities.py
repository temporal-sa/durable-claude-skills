"""Activities for the money-transfer skill.

Activities are where the non-deterministic, side-effecting work lives: every call
that touches the bank goes through one of these. They are plain sync functions
(the SDK runs them on a thread pool), which keeps them easy to test and debug.

The financial logic never runs through the language model. The agent decides
*whether* and *what* to transfer; these activities decide nothing and simply
execute against the bank, deterministically and idempotently.
"""

from __future__ import annotations

from temporalio import activity

from banking import service as bank
from skills.money_transfer.shared import (
    LedgerInput,
    TransferPlan,
    TransferRequest,
)


@activity.defn
def build_transfer_plan(request: TransferRequest) -> TransferPlan:
    """Validate the request and compute fees against current balances.

    Business problems (unknown account, insufficient funds, frozen account, over
    limit) come back as a plan with ``valid=False`` and a list of ``problems`` so
    the agent can explain them in plain language. We do not raise for these; an
    invalid plan is a normal, expected outcome, not a workflow failure.
    """
    problems = bank.validate_transfer(
        request.source_account, request.target_account, request.amount_cents
    )
    fee = bank.calculate_fee(request.amount_cents)
    source = bank.get_account(request.source_account)

    plan = TransferPlan(
        source_account=request.source_account,
        target_account=request.target_account,
        amount_cents=request.amount_cents,
        fee_cents=fee.fee_cents,
        total_debit_cents=fee.total_debit_cents,
        source_balance_before_cents=source.balance_cents if source else None,
        valid=len(problems) == 0,
        problems=problems,
    )
    activity.logger.info(
        "Built transfer plan ref=%s valid=%s problems=%d",
        request.reference_id,
        plan.valid,
        len(problems),
    )
    return plan


@activity.defn
def withdraw(input: LedgerInput) -> str:
    """Debit the source account. Raises a non-retryable error if it cannot."""
    txn_id = bank.withdraw(
        account_id=input.account_id,
        amount_cents=input.amount_cents,
        reference_id=input.reference_id,
        idempotency_key=input.idempotency_key,
    )
    activity.logger.info("Withdrew %d cents -> txn %s", input.amount_cents, txn_id)
    return txn_id


@activity.defn
def deposit(input: LedgerInput) -> str:
    """Credit the destination account. Raises a non-retryable error if it cannot."""
    txn_id = bank.deposit(
        account_id=input.account_id,
        amount_cents=input.amount_cents,
        reference_id=input.reference_id,
        idempotency_key=input.idempotency_key,
    )
    activity.logger.info("Deposited %d cents -> txn %s", input.amount_cents, txn_id)
    return txn_id


@activity.defn
def refund(input: LedgerInput) -> str | None:
    """Compensation: return a withdrawal to the source. No-op if nothing was withdrawn."""
    txn_id = bank.refund(
        account_id=input.account_id,
        amount_cents=input.amount_cents,
        reference_id=input.reference_id,
        idempotency_key=input.idempotency_key,
    )
    if txn_id is None:
        activity.logger.info("Refund ref=%s was a no-op (nothing withdrawn)", input.reference_id)
    else:
        activity.logger.info("Refunded %d cents -> txn %s", input.amount_cents, txn_id)
    return txn_id
