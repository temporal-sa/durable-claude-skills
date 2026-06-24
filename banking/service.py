"""A small, file-backed simulated bank.

This stands in for a real core-banking system / payments processor. It is the
single system of record for account balances and transactions, and it is shared
by two processes:

  * the Temporal worker, whose Activities call withdraw/deposit/refund, and
  * the agent API, whose read-only tools call lookup/quote.

State lives in a SQLite file (stdlib only) so the two processes see the same
balances. Every money-moving call takes an idempotency key so that Temporal's
automatic retries can never double-apply an effect.

Amounts are integer cents everywhere to avoid floating-point money bugs.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

DB_PATH = os.environ.get("BANK_DB_PATH", os.path.join(os.getcwd(), "banking.db"))

# ----------------------------------------------------------------------------
# Domain errors. The workflow lists these by name in non_retryable_error_types,
# so a bad transfer fails fast instead of being retried.
# ----------------------------------------------------------------------------


class BankError(Exception):
    """Base class for all banking errors."""


class InvalidAccountError(BankError):
    """The account does not exist."""


class InsufficientFundsError(BankError):
    """The source account does not have enough money."""


class AccountFrozenError(BankError):
    """The account cannot send or receive funds right now."""


class TransferLimitError(BankError):
    """The transfer is above the per-transaction limit."""


# Per-transaction ceiling for this demo (in cents). $10,000.00.
PER_TRANSFER_LIMIT_CENTS = 10_000_00


@dataclass(frozen=True)
class Account:
    account_id: str
    name: str
    kind: str  # "checking" | "savings"
    balance_cents: int
    status: str  # "active" | "frozen"


@dataclass(frozen=True)
class FeeQuote:
    amount_cents: int
    fee_cents: int
    total_debit_cents: int


@dataclass(frozen=True)
class Transaction:
    """A single ledger entry — money that actually moved, written by an Activity."""

    txn_id: str
    account_id: str
    kind: str  # withdraw | deposit | refund
    amount_cents: int
    reference_id: str
    created_at: str


# ----------------------------------------------------------------------------
# Connection handling
# ----------------------------------------------------------------------------

_local = threading.local()


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """One connection per thread, WAL mode so the worker and API can interleave."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        _local.conn = conn
    yield conn


def init_db() -> None:
    """Create tables if they do not exist. Safe to call repeatedly."""
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                account_id   TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                kind         TEXT NOT NULL,
                balance_cents INTEGER NOT NULL,
                status       TEXT NOT NULL DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS ledger (
                txn_id        TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE NOT NULL,
                account_id    TEXT NOT NULL,
                kind          TEXT NOT NULL,        -- withdraw | deposit | refund
                amount_cents  INTEGER NOT NULL,
                reference_id  TEXT NOT NULL,
                created_at    TEXT NOT NULL
            );
            """
        )
        conn.commit()


# Seed accounts. The mix is chosen so every demo path is reachable:
#   85-150  healthy checking         -> happy path
#   43-812  healthy savings          -> happy path target
#   22-019  nearly empty checking    -> insufficient funds
#   55-200  frozen account           -> deposit fails -> saga refund
#   (99-999 intentionally absent)    -> invalid account
_SEED = [
    ("85-150", "Ada's Checking", "checking", 5_000_00, "active"),
    ("43-812", "Ada's Savings", "savings", 1_200_00, "active"),
    ("22-019", "Side Hustle Checking", "checking", 250_00, "active"),
    ("55-200", "Frozen Escrow", "checking", 9_000_00, "frozen"),
]


def seed(reset: bool = False) -> None:
    """Populate demo accounts. With reset=True, wipe balances and the ledger."""
    init_db()
    with _conn() as conn:
        if reset:
            conn.execute("DELETE FROM ledger;")
            conn.execute("DELETE FROM accounts;")
        for account_id, name, kind, balance, status in _SEED:
            conn.execute(
                "INSERT OR IGNORE INTO accounts "
                "(account_id, name, kind, balance_cents, status) VALUES (?, ?, ?, ?, ?)",
                (account_id, name, kind, balance, status),
            )
        conn.commit()


# ----------------------------------------------------------------------------
# Read-only operations (safe to call from the agent API)
# ----------------------------------------------------------------------------


def get_account(account_id: str) -> Account | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
    if row is None:
        return None
    return Account(
        account_id=row["account_id"],
        name=row["name"],
        kind=row["kind"],
        balance_cents=row["balance_cents"],
        status=row["status"],
    )


def require_account(account_id: str) -> Account:
    account = get_account(account_id)
    if account is None:
        raise InvalidAccountError(f"No account found with id {account_id!r}")
    return account


def set_status(account_id: str, status: str) -> None:
    """Admin helper: freeze or reactivate an account."""
    with _conn() as conn:
        conn.execute(
            "UPDATE accounts SET status = ? WHERE account_id = ?", (status, account_id)
        )
        conn.commit()


def list_transactions(limit: int = 50) -> list[Transaction]:
    """Recent ledger entries, newest first.

    This is the bank's record of money that actually moved. Every row was written
    by a workflow Activity (withdraw / deposit / refund), so it reflects executed
    transfers, not anything the assistant merely talked about.
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT txn_id, account_id, kind, amount_cents, reference_id, created_at "
            "FROM ledger ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        Transaction(
            txn_id=r["txn_id"],
            account_id=r["account_id"],
            kind=r["kind"],
            amount_cents=r["amount_cents"],
            reference_id=r["reference_id"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def calculate_fee(amount_cents: int) -> FeeQuote:
    """Deterministic fee schedule: free up to $1,000, then 0.5% capped at $25."""
    if amount_cents <= 1_000_00:
        fee = 0
    else:
        fee = min(round(amount_cents * 0.005), 25_00)
    return FeeQuote(
        amount_cents=amount_cents,
        fee_cents=fee,
        total_debit_cents=amount_cents + fee,
    )


def validate_transfer(
    source_account: str, target_account: str, amount_cents: int
) -> list[str]:
    """Return a list of human-readable problems. Empty list means the transfer is OK.

    This is pure validation against current state; it never moves money. Both the
    agent's read-only quote tool and the workflow's planning Activity call it, so
    the conversation and the durable execution agree on what is allowed.
    """
    problems: list[str] = []

    if amount_cents <= 0:
        problems.append("The amount must be greater than zero.")
    if amount_cents > PER_TRANSFER_LIMIT_CENTS:
        problems.append(
            f"The amount exceeds the per-transfer limit of "
            f"${PER_TRANSFER_LIMIT_CENTS / 100:,.2f}."
        )

    if source_account == target_account:
        problems.append("The source and destination accounts are the same.")

    source = get_account(source_account)
    if source is None:
        problems.append(f"Source account {source_account} was not found.")
    else:
        if source.status != "active":
            problems.append(f"Source account {source_account} is {source.status}.")
        fee = calculate_fee(max(amount_cents, 0))
        if amount_cents > 0 and source.balance_cents < fee.total_debit_cents:
            problems.append(
                f"Source account {source_account} has "
                f"${source.balance_cents / 100:,.2f}, which is short of the "
                f"${fee.total_debit_cents / 100:,.2f} needed (amount plus fee)."
            )

    target = get_account(target_account)
    if target is None:
        problems.append(f"Destination account {target_account} was not found.")
    elif target.status != "active":
        problems.append(f"Destination account {target_account} is {target.status}.")

    return problems


# ----------------------------------------------------------------------------
# Money-moving operations (called only from Temporal Activities)
#
# Each takes an idempotency_key. If the same key is seen twice (because Temporal
# retried the Activity after a crash), the effect is applied exactly once and the
# original transaction id is returned.
# ----------------------------------------------------------------------------


def _existing_txn(conn: sqlite3.Connection, idempotency_key: str) -> str | None:
    row = conn.execute(
        "SELECT txn_id FROM ledger WHERE idempotency_key = ?", (idempotency_key,)
    ).fetchone()
    return row["txn_id"] if row else None


def _record(
    conn: sqlite3.Connection,
    *,
    idempotency_key: str,
    account_id: str,
    kind: str,
    amount_cents: int,
    reference_id: str,
) -> str:
    txn_id = f"{kind[:1]}-{reference_id}-{idempotency_key[-6:]}"
    conn.execute(
        "INSERT INTO ledger "
        "(txn_id, idempotency_key, account_id, kind, amount_cents, reference_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            txn_id,
            idempotency_key,
            account_id,
            kind,
            amount_cents,
            reference_id,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return txn_id


def withdraw(
    account_id: str, amount_cents: int, reference_id: str, idempotency_key: str
) -> str:
    with _conn() as conn:
        existing = _existing_txn(conn, idempotency_key)
        if existing:
            return existing  # already applied on a previous attempt

        account = conn.execute(
            "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        if account is None:
            raise InvalidAccountError(f"No account found with id {account_id!r}")
        if account["status"] != "active":
            raise AccountFrozenError(f"Account {account_id} is {account['status']}")
        if account["balance_cents"] < amount_cents:
            raise InsufficientFundsError(
                f"Account {account_id} has {account['balance_cents']} cents, "
                f"cannot withdraw {amount_cents}"
            )

        conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents - ? WHERE account_id = ?",
            (amount_cents, account_id),
        )
        txn_id = _record(
            conn,
            idempotency_key=idempotency_key,
            account_id=account_id,
            kind="withdraw",
            amount_cents=amount_cents,
            reference_id=reference_id,
        )
        conn.commit()
        return txn_id


def deposit(
    account_id: str, amount_cents: int, reference_id: str, idempotency_key: str
) -> str:
    with _conn() as conn:
        existing = _existing_txn(conn, idempotency_key)
        if existing:
            return existing

        account = conn.execute(
            "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        if account is None:
            raise InvalidAccountError(f"No account found with id {account_id!r}")
        if account["status"] != "active":
            raise AccountFrozenError(f"Account {account_id} is {account['status']}")

        conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents + ? WHERE account_id = ?",
            (amount_cents, account_id),
        )
        txn_id = _record(
            conn,
            idempotency_key=idempotency_key,
            account_id=account_id,
            kind="deposit",
            amount_cents=amount_cents,
            reference_id=reference_id,
        )
        conn.commit()
        return txn_id


def refund(
    account_id: str, amount_cents: int, reference_id: str, idempotency_key: str
) -> str | None:
    """Compensation for a withdrawal.

    This is the saga compensation, so it is registered *before* the withdrawal
    runs and must be safe in every ordering:

      * If the withdrawal for this reference never committed, there is nothing to
        return, so this is a no-op and returns None.
      * If it did commit, the funds are credited back exactly once (idempotent on
        idempotency_key).
    """
    withdraw_key = f"withdraw:{reference_id}"
    with _conn() as conn:
        existing = _existing_txn(conn, idempotency_key)
        if existing:
            return existing

        withdrew = conn.execute(
            "SELECT 1 FROM ledger WHERE idempotency_key = ?", (withdraw_key,)
        ).fetchone()
        if withdrew is None:
            return None  # nothing was ever withdrawn; nothing to refund

        conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents + ? WHERE account_id = ?",
            (amount_cents, account_id),
        )
        txn_id = _record(
            conn,
            idempotency_key=idempotency_key,
            account_id=account_id,
            kind="refund",
            amount_cents=amount_cents,
            reference_id=reference_id,
        )
        conn.commit()
        return txn_id
