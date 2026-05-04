from datetime import date

from cardkaki.periods import days_left, period_bounds, period_label


def test_calendar_month_bounds_jan():
    assert period_bounds("calendar_month", date(2026, 1, 15)) == (
        date(2026, 1, 1),
        date(2026, 2, 1),
    )


def test_calendar_month_bounds_dec_rolls_year():
    assert period_bounds("calendar_month", date(2026, 12, 5)) == (
        date(2026, 12, 1),
        date(2027, 1, 1),
    )


def test_calendar_quarter_bounds_q2():
    assert period_bounds("calendar_quarter", date(2026, 4, 15)) == (
        date(2026, 4, 1),
        date(2026, 7, 1),
    )


def test_calendar_quarter_bounds_q4_rolls_year():
    assert period_bounds("calendar_quarter", date(2026, 11, 30)) == (
        date(2026, 10, 1),
        date(2027, 1, 1),
    )


def test_days_left_end_of_month():
    # May 31 is the last day of May; days_left should be 1 (end is Jun 1).
    assert days_left("calendar_month", date(2026, 5, 31)) == 1


def test_days_left_first_of_month():
    assert days_left("calendar_month", date(2026, 5, 1)) == 31


def test_statement_month_uses_statement_day_mid_cycle():
    # statement closes on 22; today=May 5 is in the cycle that closes May 22.
    # Cycle = [Apr 23, May 23).
    assert period_bounds("statement_month", date(2026, 5, 5), statement_day=22) == (
        date(2026, 4, 23),
        date(2026, 5, 23),
    )


def test_statement_month_uses_statement_day_after_close():
    # today=May 23 is in the cycle that closes Jun 22.
    assert period_bounds("statement_month", date(2026, 5, 23), statement_day=22) == (
        date(2026, 5, 23),
        date(2026, 6, 23),
    )


def test_statement_month_on_close_day():
    # today=May 22 is the LAST day of the May statement cycle.
    start, end = period_bounds("statement_month", date(2026, 5, 22), statement_day=22)
    assert start == date(2026, 4, 23)
    assert end == date(2026, 5, 23)


def test_statement_month_falls_back_to_calendar_when_unset():
    assert period_bounds("statement_month", date(2026, 5, 15)) == (
        date(2026, 5, 1),
        date(2026, 6, 1),
    )


def test_statement_month_jan_back_to_dec():
    # statement_day=15, today=Jan 5 → cycle is Dec 16 prev year — Jan 16 this year.
    assert period_bounds("statement_month", date(2026, 1, 5), statement_day=15) == (
        date(2025, 12, 16),
        date(2026, 1, 16),
    )


def test_statement_quarter_uses_statement_day():
    # statement closes on 22; today=Apr 15 → cycle was [Jan 23, Apr 23).
    start, end = period_bounds("statement_quarter", date(2026, 4, 15), statement_day=22)
    assert start == date(2026, 1, 23)
    assert end == date(2026, 4, 23)


def test_period_label_calendar_month():
    assert period_label("calendar_month", date(2026, 5, 15)) == "calendar month"


def test_period_label_statement_month_with_day():
    label = period_label("statement_month", date(2026, 5, 5), statement_day=22)
    assert "23 Apr" in label
    assert "22 May" in label


def test_period_label_statement_month_without_day():
    label = period_label("statement_month", date(2026, 5, 5))
    assert "not set" in label.lower()


def test_safe_date_handles_feb_clamp():
    # statement_day=28, today=Jan 5 → cycle goes back to Dec 29..Jan 29
    # When stepping forward past Feb, day 29 might overshoot. Spot-check Feb edge.
    start, end = period_bounds("statement_month", date(2026, 2, 5), statement_day=28)
    # cycle = [Jan 29, Feb 29) — Feb 29 doesn't exist in 2026, clamps to Feb 28.
    assert start == date(2026, 1, 29)
    assert end == date(2026, 2, 28)
