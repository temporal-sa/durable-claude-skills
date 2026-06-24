"""Dependency-free smoke test for the bank's money invariants.

This does not need Temporal, Anthropic, or any third-party package — just the
standard library — so it runs anywhere. It checks the financial core the workflow
relies on: fees, validation, idempotency, and the withdraw -> deposit -> refund
saga sequence.

    python scripts/smoke_test.py

The full behavioural suite (the actual workflow, approval gate, and saga running
inside Temporal) lives in skills/money_transfer/tests and is run with pytest.
"""

import os
import sys
import tempfile

# Make the repo root importable regardless of where this is run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point the bank at a throwaway DB before importing it.
os.environ["BANK_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="smoke-"), "bank.db")

from banking import service as bank  # noqa: E402

PASS = 0


def check(label: str, condition: bool) -> None:
    global PASS
    if not condition:
        raise AssertionError(f"FAILED: {label}")
    PASS += 1
    print(f"  ok  {label}")


def main() -> None:
    bank.seed(reset=True)

    print("Fees")
    check("no fee under $1,000", bank.calculate_fee(100_00).fee_cents == 0)
    check("0.5% fee at $1,500 is $7.50", bank.calculate_fee(1_500_00).fee_cents == 750)
    check("fee capped at $25 for large amounts", bank.calculate_fee(50_000_00).fee_cents == 25_00)

    print("Validation (no money moves)")
    check("a good transfer has no problems", bank.validate_transfer("85-150", "43-812", 100_00) == [])
    check("unknown destination is caught", any("99-999" in p for p in bank.validate_transfer("85-150", "99-999", 100_00)))
    check("frozen destination is caught", any("frozen" in p for p in bank.validate_transfer("85-150", "55-200", 100_00)))
    check("insufficient funds is caught", any("short" in p for p in bank.validate_transfer("22-019", "43-812", 5_000_00)))
    check("same-account is caught", any("same" in p for p in bank.validate_transfer("85-150", "85-150", 100_00)))
    check("over-limit is caught", any("limit" in p for p in bank.validate_transfer("85-150", "43-812", 20_000_00)))

    print("Idempotency")
    t1 = bank.withdraw("85-150", 100_00, "r1", "withdraw:r1")
    t1_again = bank.withdraw("85-150", 100_00, "r1", "withdraw:r1")  # simulated retry
    check("repeated withdraw applies once", t1 == t1_again)
    check("balance debited once", bank.get_account("85-150").balance_cents == 4_900_00)
    bank.deposit("43-812", 100_00, "r1", "deposit:r1")
    check("deposit credited", bank.get_account("43-812").balance_cents == 1_300_00)

    print("Refund behaviour")
    check("refund is a no-op when nothing was withdrawn", bank.refund("85-150", 100_00, "rX", "refund:rX") is None)
    check("no-op refund changes no balance", bank.get_account("85-150").balance_cents == 4_900_00)
    check("refund credits back a real withdrawal", bank.refund("85-150", 100_00, "r1", "refund:r1") is not None)
    check("source restored after refund", bank.get_account("85-150").balance_cents == 5_000_00)

    print("Saga sequence (mirrors the workflow on a frozen destination)")
    bank.seed(reset=True)
    plan = bank.calculate_fee(100_00)  # fee 0, total 100
    wd = bank.withdraw("85-150", plan.total_debit_cents, "r2", "withdraw:r2")
    check("withdraw succeeds", wd is not None and bank.get_account("85-150").balance_cents == 4_900_00)
    deposit_failed = False
    try:
        bank.deposit("55-200", 100_00, "r2", "deposit:r2")  # 55-200 is frozen
    except bank.AccountFrozenError:
        deposit_failed = True
    check("deposit to frozen account fails", deposit_failed)
    rf = bank.refund("85-150", plan.total_debit_cents, "r2", "refund:r2")
    check("compensation refunds the full debit", rf is not None)
    check("source is made whole", bank.get_account("85-150").balance_cents == 5_000_00)
    check("frozen destination received nothing", bank.get_account("55-200").balance_cents == 9_000_00)

    print("Guards raise the right errors")
    froze = False
    try:
        bank.withdraw("55-200", 10_00, "r3", "withdraw:r3")
    except bank.AccountFrozenError:
        froze = True
    check("withdraw from frozen raises AccountFrozenError", froze)
    short = False
    try:
        bank.withdraw("22-019", 1_000_00, "r4", "withdraw:r4")
    except bank.InsufficientFundsError:
        short = True
    check("over-balance withdraw raises InsufficientFundsError", short)

    print(f"\nALL {PASS} CHECKS PASSED")


if __name__ == "__main__":
    main()
