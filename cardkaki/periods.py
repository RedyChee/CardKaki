"""Period date math for cap and min-spend tracking.

`statement_*` periods need a per-user `statement_day` (closing day of the
billing cycle, 1..28). When `statement_day` is None we fall back to the
calendar variant — useful when a user hasn't told us their cycle yet.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Literal

PeriodLiteral = Literal[
    "calendar_month",
    "statement_month",
    "calendar_quarter",
    "statement_quarter",
]


def _safe_date(year: int, month: int, day: int) -> date:
    """Clamp day to last valid day of month (handles Feb 29/30/31)."""
    if month <= 0:
        month += 12
        year -= 1
    elif month >= 13:
        month -= 12
        year += 1
    return date(year, month, min(day, monthrange(year, month)[1]))


def _calendar_month_bounds(today: date) -> tuple[date, date]:
    start = date(today.year, today.month, 1)
    end = _safe_date(today.year, today.month + 1, 1)
    return start, end


def _statement_month_bounds(today: date, statement_day: int) -> tuple[date, date]:
    # Cycle ends on statement_day (inclusive); convert to half-open [start, end).
    # Cycle for spend posting on `today`:
    #   if today.day <= statement_day  → cycle ends statement_day THIS month
    #   if today.day >  statement_day  → cycle ends statement_day NEXT month
    if today.day <= statement_day:
        end = _safe_date(today.year, today.month, statement_day + 1)
        start = _safe_date(today.year, today.month - 1, statement_day + 1)
    else:
        start = _safe_date(today.year, today.month, statement_day + 1)
        end = _safe_date(today.year, today.month + 1, statement_day + 1)
    return start, end


def _calendar_quarter_bounds(today: date) -> tuple[date, date]:
    q_start_month = ((today.month - 1) // 3) * 3 + 1
    start = date(today.year, q_start_month, 1)
    end = _safe_date(today.year, q_start_month + 3, 1)
    return start, end


def _statement_quarter_bounds(today: date, statement_day: int) -> tuple[date, date]:
    # Approx: same window logic as statement_month, scaled to 3 months.
    # No card in cards.yaml uses statement_quarter today; this is a future-proof
    # implementation that follows the same cycle-day convention.
    if today.day <= statement_day:
        end = _safe_date(today.year, today.month, statement_day + 1)
        start = _safe_date(today.year, today.month - 3, statement_day + 1)
    else:
        start = _safe_date(today.year, today.month - 2, statement_day + 1)
        end = _safe_date(today.year, today.month + 1, statement_day + 1)
    return start, end


def period_bounds(
    period: PeriodLiteral,
    today: date,
    statement_day: int | None = None,
) -> tuple[date, date]:
    """Return [start, end) — end exclusive — for the period containing `today`.

    statement_* falls back to calendar_* when statement_day is None.
    """
    if period == "calendar_month":
        return _calendar_month_bounds(today)
    if period == "statement_month":
        if statement_day is None:
            return _calendar_month_bounds(today)
        return _statement_month_bounds(today, statement_day)
    if period == "calendar_quarter":
        return _calendar_quarter_bounds(today)
    if period == "statement_quarter":
        if statement_day is None:
            return _calendar_quarter_bounds(today)
        return _statement_quarter_bounds(today, statement_day)
    raise ValueError(f"unknown period: {period!r}")


def days_left(
    period: PeriodLiteral,
    today: date,
    statement_day: int | None = None,
) -> int:
    """Days remaining in the current cycle, including today (1 = last day)."""
    _, end = period_bounds(period, today, statement_day)
    return max(0, (end - today).days)


_MONTHS_SHORT = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]


def _fmt(d: date) -> str:
    return f"{d.day} {_MONTHS_SHORT[d.month - 1]}"


def period_label(
    period: PeriodLiteral,
    today: date,
    statement_day: int | None = None,
) -> str:
    """Human-readable label for the current cycle."""
    if period == "calendar_month":
        return "calendar month"
    if period == "calendar_quarter":
        return "calendar quarter"
    if statement_day is None:
        kind = "month" if "month" in period else "quarter"
        return f"calendar {kind} (statement day not set)"
    start, end = period_bounds(period, today, statement_day)
    last = end - timedelta(days=1)
    label = "statement" if "month" in period else "statement-quarter"
    return f"{label} ({_fmt(start)} — {_fmt(last)})"
