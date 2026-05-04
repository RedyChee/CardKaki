from datetime import date

import pytest

from cardkaki.posting import posting_period_warning, resolve_posting_date


# ---------------------------------------------------------------------------
# resolve_posting_date
# ---------------------------------------------------------------------------


def test_no_delay_returns_txn_date():
    assert resolve_posting_date(date(2026, 5, 4), delay_days=0) == date(2026, 5, 4)


def test_t1_delay_next_day():
    assert resolve_posting_date(date(2026, 5, 4), delay_days=1) == date(2026, 5, 5)


def test_t2_delay():
    assert resolve_posting_date(date(2026, 5, 4), delay_days=2) == date(2026, 5, 6)


def test_t3_delay():
    assert resolve_posting_date(date(2026, 5, 4), delay_days=3) == date(2026, 5, 7)


def test_friday_t1_advances_to_monday():
    # Friday 1 May + T+1 = Saturday → skip to Monday 4 May
    assert resolve_posting_date(date(2026, 5, 1), delay_days=1) == date(2026, 5, 4)


def test_friday_t2_advances_to_tuesday():
    # Friday 1 May + T+2 = Sunday → skip to Monday 4 May
    assert resolve_posting_date(date(2026, 5, 1), delay_days=2) == date(2026, 5, 4)


def test_saturday_t1_advances_past_sunday():
    # Saturday 2 May + T+1 = Sunday → skip to Monday 4 May
    assert resolve_posting_date(date(2026, 5, 2), delay_days=1) == date(2026, 5, 4)


def test_same_day_merchant_overrides_delay():
    # Even with T+3, same-day merchant posts on txn_date
    assert resolve_posting_date(date(2026, 5, 4), delay_days=3, same_day_merchant=True) == date(2026, 5, 4)


def test_same_day_merchant_on_friday_stays_friday():
    # Friday txn, same-day posting — no weekend skip
    assert resolve_posting_date(date(2026, 5, 1), delay_days=2, same_day_merchant=True) == date(2026, 5, 1)


# ---------------------------------------------------------------------------
# posting_period_warning
# ---------------------------------------------------------------------------


def test_no_warning_same_period():
    # Apr 28, T+1 → Apr 29, same calendar month → no warning
    warning = posting_period_warning(
        txn_date=date(2026, 4, 28),
        posting_date=date(2026, 4, 29),
        period="calendar_month",
        statement_day=None,
    )
    assert warning is None


def test_warning_crosses_calendar_month_boundary():
    # Apr 30, T+1 → May 1 — different calendar month
    warning = posting_period_warning(
        txn_date=date(2026, 4, 30),
        posting_date=date(2026, 5, 1),
        period="calendar_month",
        statement_day=None,
    )
    assert warning is not None
    assert "May" in warning
    assert "Apr" in warning


def test_warning_contains_day_name_and_date():
    warning = posting_period_warning(
        txn_date=date(2026, 4, 30),
        posting_date=date(2026, 5, 1),
        period="calendar_month",
        statement_day=None,
    )
    assert "Fri" in warning   # May 1 2026 is a Friday
    assert "1 May" in warning


def test_no_warning_crosses_boundary_but_same_posting_period():
    # Statement month closes on 15; txn_date=Apr 14, posting=Apr 15 is still in
    # the same statement cycle (cycle ends Apr 16 exclusive i.e. end=Apr 16)
    # Actually: statement_day=15, today=Apr 14 → cycle = [Mar 16, Apr 16)
    # Apr 14 is in [Mar 16, Apr 16); Apr 15 is also in [Mar 16, Apr 16)
    warning = posting_period_warning(
        txn_date=date(2026, 4, 14),
        posting_date=date(2026, 4, 15),
        period="statement_month",
        statement_day=15,
    )
    assert warning is None


def test_warning_crosses_statement_month_boundary():
    # statement_day=15; txn=Apr 15, posts Apr 16 → crosses into next cycle
    warning = posting_period_warning(
        txn_date=date(2026, 4, 15),
        posting_date=date(2026, 4, 16),
        period="statement_month",
        statement_day=15,
    )
    assert warning is not None


def test_warning_anniversary_year_same_period():
    # anniversary_month=5, txn=Apr 30, posting=May 1 → boundary!
    warning = posting_period_warning(
        txn_date=date(2026, 4, 30),
        posting_date=date(2026, 5, 1),
        period="anniversary_year",
        statement_day=None,
        anniversary_month=5,
    )
    assert warning is not None
    assert "May" in warning
