"""Pure rollup: transactions → per-(card, bonus) period spend totals.

The engine consumes the output as `usage` and decides cap/min-spend gating.
This module has no I/O — bot/storage layers feed it pre-fetched data.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable

from .models import BonusUsage, Card, TxnRow
from .periods import period_bounds


def build_usage(
    txns: Iterable[TxnRow],
    user_cards: Iterable[Card],
    today: date,
    statement_days: dict[str, int] | None = None,
) -> dict[tuple[str, int], BonusUsage]:
    """For each (card_id, bonus_idx) the user owns, sum txns logged with
    that bonus_idx that fall within the bonus's cap_period and (separately)
    min_spend_period windows.

    Txns are matched by bonus_idx — the engine tagged them at log time.
    Past txns thus stick to the bonus they qualified for, even if cards.yaml
    later changes.
    """
    statement_days = statement_days or {}
    txns_list = list(txns)
    out: dict[tuple[str, int], BonusUsage] = {}

    for card in user_cards:
        s_day = statement_days.get(card.id)
        for idx, bonus in enumerate(card.bonus):
            cap_period = bonus.cap_period or "calendar_month"
            ms_period = bonus.min_spend_period or "calendar_month"

            cap_start, cap_end = period_bounds(cap_period, today, s_day)
            ms_start, ms_end = period_bounds(ms_period, today, s_day)

            spend = 0.0
            min_spend = 0.0
            for t in txns_list:
                if t.card_id != card.id or t.bonus_idx != idx:
                    continue
                if cap_start <= t.txn_date < cap_end:
                    spend += t.amount_sgd
                if ms_start <= t.txn_date < ms_end:
                    min_spend += t.amount_sgd

            out[(card.id, idx)] = BonusUsage(
                spend_sgd=spend, min_spend_sgd=min_spend
            )

    return out
