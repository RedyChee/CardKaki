from datetime import date, datetime
from pathlib import Path

import pytest

from cardkaki.data import load_cards
from cardkaki.models import TxnRow
from cardkaki.usage import build_usage

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="module")
def cards():
    return load_cards(DATA / "cards.yaml")


def _txn(card_id, bonus_idx, amount, txn_date, is_fcy=False, merchant="x"):
    return TxnRow(
        tx_id="t" + str(amount),
        telegram_user_id=1,
        card_id=card_id,
        bonus_idx=bonus_idx,
        bonus_label=None,
        merchant=merchant,
        amount_sgd=float(amount),
        is_fcy=is_fcy,
        miles_earned=0,
        txn_date=txn_date,
        created_at=datetime(2026, 5, 4, 12, 0, 0),
    )


def test_empty_txns_yields_zero_for_each_owned_bonus(cards):
    wallet = [cards["uob_ppv"]]
    usage = build_usage([], wallet, today=date(2026, 5, 4))
    # PPV has 2 bonuses
    assert usage[("uob_ppv", 0)].spend_sgd == 0
    assert usage[("uob_ppv", 0)].min_spend_sgd == 0
    assert usage[("uob_ppv", 1)].spend_sgd == 0


def test_sum_qualifying_only_by_bonus_idx(cards):
    wallet = [cards["uob_ppv"]]
    txns = [
        _txn("uob_ppv", 0, 100, date(2026, 5, 1)),  # bonus_idx=0
        _txn("uob_ppv", 0, 50, date(2026, 5, 2)),
        _txn("uob_ppv", 1, 200, date(2026, 5, 3)),  # bonus_idx=1
    ]
    usage = build_usage(txns, wallet, today=date(2026, 5, 4))
    assert usage[("uob_ppv", 0)].spend_sgd == 150
    assert usage[("uob_ppv", 1)].spend_sgd == 200


def test_period_boundary_calendar_month(cards):
    wallet = [cards["uob_ppv"]]
    txns = [
        _txn("uob_ppv", 0, 100, date(2026, 4, 30)),  # last day of April
        _txn("uob_ppv", 0, 50, date(2026, 5, 1)),  # May 1
    ]
    usage = build_usage(txns, wallet, today=date(2026, 5, 15))
    # Only May txns count
    assert usage[("uob_ppv", 0)].spend_sgd == 50


def test_period_boundary_statement_month_with_statement_day(cards):
    wallet = [cards["uob_vs"]]
    statement_days = {"uob_vs": 22}
    txns = [
        # Apr 23 — May 22 cycle
        _txn("uob_vs", 0, 200, date(2026, 4, 23), is_fcy=True),
        _txn("uob_vs", 0, 300, date(2026, 5, 22), is_fcy=True),
        # Outside cycle (next cycle starts May 23)
        _txn("uob_vs", 0, 999, date(2026, 5, 23), is_fcy=True),
        # Outside cycle (prev cycle)
        _txn("uob_vs", 0, 444, date(2026, 4, 22), is_fcy=True),
    ]
    usage = build_usage(txns, wallet, today=date(2026, 5, 5), statement_days=statement_days)
    assert usage[("uob_vs", 0)].spend_sgd == 500


def test_min_spend_period_separate_from_cap_period(cards):
    # Maybank XL: cap_period and min_spend_period are both calendar_month
    # so they should match. Use it to spot-check that build_usage computes
    # both totals from the same window.
    wallet = [cards["maybank_xl"]]
    txns = [
        _txn("maybank_xl", 0, 200, date(2026, 5, 1)),
        _txn("maybank_xl", 0, 150, date(2026, 5, 10)),
    ]
    usage = build_usage(txns, wallet, today=date(2026, 5, 15))
    assert usage[("maybank_xl", 0)].spend_sgd == 350
    assert usage[("maybank_xl", 0)].min_spend_sgd == 350


def test_no_other_user_card_in_usage_dict(cards):
    # Only owned cards' bonuses appear in usage.
    wallet = [cards["uob_ppv"]]
    usage = build_usage([], wallet, today=date(2026, 5, 4))
    keys = set(usage.keys())
    # PPV has 2 bonuses, no other cards.
    assert keys == {("uob_ppv", 0), ("uob_ppv", 1)}
