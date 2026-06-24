"""Reset the simulated bank to its starting balances.

    python scripts/seed_bank.py          # create accounts if missing
    python scripts/seed_bank.py --reset  # wipe balances and ledger, then reseed
"""

import sys

from banking import service as bank


def main() -> None:
    reset = "--reset" in sys.argv
    bank.seed(reset=reset)
    print(f"Bank seeded (reset={reset}). Accounts:")
    for account_id in ("85-150", "43-812", "22-019", "55-200"):
        account = bank.get_account(account_id)
        if account:
            print(
                f"  {account.account_id}  {account.name:<22} "
                f"${account.balance_cents / 100:>10,.2f}  [{account.status}]"
            )


if __name__ == "__main__":
    main()
