"""Posting date prediction and period-boundary warnings.

Pure utilities — no I/O. All inputs come from the caller.
"""
from __future__ import annotations

from datetime import date, timedelta

from .periods import PeriodLiteral, _MONTHS_SHORT, period_bounds


def resolve_posting_date(
    txn_date: date,
    delay_days: int,
    same_day_merchant: bool = False,
) -> date:
    """Predict the posting date, advancing past weekends.

    If same_day_merchant is True, the merchant is known to post on the
    transaction date regardless of issuer lag.
    """
    effective_delay = 0 if same_day_merchant else delay_days
    d = txn_date + timedelta(days=effective_delay)
    while d.isoweekday() > 5:   # 6=Sat, 7=Sun
        d += timedelta(days=1)
    return d


def posting_period_warning(
    txn_date: date,
    posting_date: date,
    period: PeriodLiteral,
    statement_day: int | None,
    anniversary_month: int | None = None,
) -> str | None:
    """Return a human-readable warning if posting_date lands in a different
    period than txn_date, else None.

    Example: 'Posts Fri 1 May — counts toward May cap, not Apr'
    """
    txn_start, txn_end = period_bounds(
        period, txn_date, statement_day, anniversary_month
    )
    if txn_start <= posting_date < txn_end:
        return None

    day_name = posting_date.strftime("%a")
    post_day = posting_date.day
    post_month = _MONTHS_SHORT[posting_date.month - 1]
    txn_month = _MONTHS_SHORT[txn_date.month - 1]
    return f"Posts {day_name} {post_day} {post_month} — counts toward {post_month} cap, not {txn_month}"
