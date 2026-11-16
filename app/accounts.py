"""Round-robin selection of Divar accounts for phone lookups."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class AccountView:
    id: int
    status: str
    views_today: int
    daily_cap: int
    views_day: str | None
    delay_min: float
    delay_max: float


def available(account: AccountView, today: str) -> bool:
    if account.status != "active":
        return False
    if account.views_today >= account.daily_cap:
        return False
    return True

def account_view_from_row(row: dict) -> AccountView:
    return AccountView(
        id=int(row["id"]),
        status=str(row.get("status") or "needs_login"),
        views_today=int(row.get("views_today") or 0),
        daily_cap=int(row.get("daily_cap") or 50),
        views_day=row.get("views_day"),
        delay_min=float(row.get("delay_min") or 20),
        delay_max=float(row.get("delay_max") or 40),
    )


def pick_account(accounts: list[AccountView], cursor: int, today: str) -> tuple[AccountView | None, int]:
    if not accounts:
        return None, cursor
    count = len(accounts)
    for offset in range(count):
        index = (cursor + offset) % count
        account = accounts[index]
        if available(account, today):
            return account, (index + 1) % count
    return None, cursor


def should_reset_limit(account: AccountView, today: str) -> bool:
    return account.status == "limited" and account.views_day != today
