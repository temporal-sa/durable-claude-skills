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
from temporalio.exceptions import ApplicationError

from banking import service as bank
from skills.money_transfer.shared import (
    LedgerInput,
    TransferPlan,
    TransferRequest,
)

#: Demo: the deposit activity fails until this attempt number, so Temporal's
#: automatic retries (paced 5s apart by the workflow's deposit retry policy) are
#: visible in the UI before the deposit finally succeeds. Set to 1 to disable.
DEPOSIT_SUCCEED_ON_ATTEMPT = 3


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
    """Credit the destination account.

    A closed (or otherwise non-active) destination fails here — after the source
    has already been debited — so the workflow's saga rolls the withdrawal back.
    """
    account = bank.get_account(input.account_id)

    # Demo: simulate a flaky-but-recoverable destination by failing the first
    # attempts, but only for an account that *can* eventually succeed. A closed
    # account never can, so skip the simulation and let bank.deposit fail hard
    # (non-retryable, below), which triggers the saga refund. The raised error is
    # retryable (its type is not in the workflow's non-retryable list), so
    # Temporal retries it until DEPOSIT_SUCCEED_ON_ATTEMPT. We fail before
    # touching the bank, so no money moves on the failed attempts.
    if account is not None and account.status == "active":
        attempt = activity.info().attempt
        if attempt < DEPOSIT_SUCCEED_ON_ATTEMPT:
            activity.logger.warning(
                "Simulated deposit failure on attempt %d of %d",
                attempt,
                DEPOSIT_SUCCEED_ON_ATTEMPT,
            )
            raise ApplicationError(
                f"Simulated deposit failure (attempt {attempt} of "
                f"{DEPOSIT_SUCCEED_ON_ATTEMPT})",
                type="SimulatedDepositFailure",
            )

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
