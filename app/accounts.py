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
    if account.status == "expired":
        return False
    if account.views_today >= account.daily_cap:
        return False
    if account.status == "limited" and account.views_day == today:
        return False
    return True


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
