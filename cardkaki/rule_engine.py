"""Pure rule engine: ranks cards by effective miles for a single transaction.

No I/O. All inputs come from the caller. Used by the bot, but trivially
unit-testable on its own — see tests/test_rule_engine.py.

v1 surfaces caps and min-spend in `reasons` strings only; nothing is
subtracted from running totals (that's v2). The `today` parameter is
present for forward compat per README; unused in v1.
"""
from __future__ import annotations

from datetime import date
from math import floor

from .models import Bonus, Card, Recommendation


def recommend(
    user_cards: list[Card],
    merchant_categories: list[str],
    amount_sgd: float,
    is_fcy: bool = False,
    today: date | None = None,
) -> list[Recommendation]:
    if amount_sgd <= 0:
        raise ValueError("amount_sgd must be > 0")

    merchant_set = set(merchant_categories)
    recs: list[Recommendation] = []
    for card in user_cards:
        rec = _evaluate_card(card, merchant_set, amount_sgd, is_fcy)
        recs.append(rec)

    recs.sort(key=lambda r: (-r.miles, r.card_name.lower()))
    return recs


def _evaluate_card(
    card: Card,
    merchant_set: set[str],
    amount_sgd: float,
    is_fcy: bool,
) -> Recommendation:
    reasons: list[str] = []

    # 1. Cost = what the user actually pays (incl. FCY fee).
    fcy_fee = card.fcy_fee if is_fcy else 0.0
    cost = amount_sgd * (1 + fcy_fee)

    # 2. Amount used for miles math after rounding.
    amt_for_miles, rounding_reason = _apply_rounding(card, amount_sgd)
    if rounding_reason:
        reasons.append(rounding_reason)

    # 3. Base rate (FCY-aware).
    if is_fcy and not card.applies_base_to_fcy:
        base_rate = 0.0
    elif is_fcy and card.base_rate_mpd_fcy is not None:
        base_rate = card.base_rate_mpd_fcy
    else:
        base_rate = card.base_rate_mpd

    # 4. Find best applicable bonus rate. Track the winning bonus (for ✓ reason)
    #    and any excluded bonus that would have qualified (for ⚠ reason).
    best_bonus_rate = 0.0
    best_bonus: Bonus | None = None
    excluded_reason: str | None = None

    for bonus in card.bonus:
        cat_match = (not bonus.categories) or bool(set(bonus.categories) & merchant_set)
        if not cat_match:
            continue
        # FCY/SGD scope check
        if is_fcy and not bonus.applies_to_fcy:
            continue
        if (not is_fcy) and not bonus.applies_to_sgd:
            continue
        # Exclusion veto
        excl = set(bonus.excluded_categories) & merchant_set
        if excl:
            if excluded_reason is None:
                excluded_reason = f"⚠ excluded: {sorted(excl)[0].replace('_', ' ')}"
            continue
        if bonus.rate_mpd > best_bonus_rate:
            best_bonus_rate = bonus.rate_mpd
            best_bonus = bonus

    rate = max(base_rate, best_bonus_rate)

    # 5. Miles + effective mpd.
    miles = floor(amt_for_miles * rate)
    effective_mpd = round((miles / cost) if cost > 0 else 0.0, 2)

    # 6. Reasons.
    if best_bonus is not None:
        label = best_bonus.label or "bonus"
        reasons.insert(0, f"✓ {label}")
        if best_bonus.cap_sgd is not None:
            cap_period = best_bonus.cap_period or "month"
            reasons.append(f"cap S${int(best_bonus.cap_sgd)}/{cap_period.replace('_', ' ')}")
        if best_bonus.min_spend_sgd is not None:
            ms_period = best_bonus.min_spend_period or "month"
            reasons.append(
                f"needs S${int(best_bonus.min_spend_sgd)}/{ms_period.replace('_', ' ')} min spend"
            )
    elif excluded_reason is not None:
        reasons.insert(0, excluded_reason)
    if is_fcy:
        reasons.append(f"FCY +{card.fcy_fee * 100:.2f}% fee")

    return Recommendation(
        card_id=card.id,
        card_name=card.name,
        miles=miles,
        effective_mpd=effective_mpd,
        reasons=reasons,
    )


def _apply_rounding(card: Card, amount_sgd: float) -> tuple[float, str | None]:
    method = card.rounding.method
    if method == "floor_sgd_5":
        amt = float(floor(amount_sgd / 5) * 5)
        if amt < amount_sgd:
            return amt, f"rounded S${amount_sgd:.2f} → S${int(amt)}"
        return amt, None
    if method == "floor_sgd_1":
        amt = float(floor(amount_sgd))
        if amt < amount_sgd:
            return amt, f"rounded S${amount_sgd:.2f} → S${int(amt)}"
        return amt, None
    return amount_sgd, None
