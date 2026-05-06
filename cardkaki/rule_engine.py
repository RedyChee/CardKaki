"""Pure rule engine: ranks cards by effective miles for a single transaction.

No I/O. All inputs come from the caller. Used by the bot, but trivially
unit-testable on its own — see tests/test_rule_engine.py.

When `usage` is None (v1 callers), caps and min-spend are surfaced as
informational strings only — exactly the v1 behavior. When `usage` is
provided (v2), the engine gates min-spend and blends bonus/base across
remaining cap, picking the bonus with the highest miles for this txn.
"""
from __future__ import annotations

from datetime import date
from math import floor

from .models import Bonus, BonusUsage, Card, Recommendation
from .periods import days_left, period_bounds
from .posting import posting_period_warning, resolve_posting_date


def recommend(
    user_cards: list[Card],
    merchant_categories: list[str],
    amount_sgd: float,
    is_fcy: bool = False,
    today: date | None = None,
    usage: dict[tuple[str, int], BonusUsage] | None = None,
    statement_days: dict[str, int] | None = None,
    posting_delays: dict[str, int] | None = None,
    same_day_merchant: bool = False,
    anniversary_months: dict[str, int] | None = None,
) -> list[Recommendation]:
    if amount_sgd <= 0:
        raise ValueError("amount_sgd must be > 0")

    merchant_set = set(merchant_categories)
    recs: list[Recommendation] = []
    for card in user_cards:
        if usage is None:
            rec = _evaluate_card_v1(card, merchant_set, amount_sgd, is_fcy)
        else:
            rec = _evaluate_card_v2(
                card,
                merchant_set,
                amount_sgd,
                is_fcy,
                today=today or date.today(),
                usage=usage,
                statement_days=statement_days or {},
                posting_delays=posting_delays,
                same_day_merchant=same_day_merchant,
                anniversary_months=anniversary_months,
            )
        recs.append(rec)

    recs.sort(key=lambda r: (-r.miles, r.card_name.lower()))
    return recs


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _base_rate(card: Card, is_fcy: bool) -> float:
    if is_fcy and not card.applies_base_to_fcy:
        return 0.0
    if is_fcy and card.base_rate_mpd_fcy is not None:
        return card.base_rate_mpd_fcy
    return card.base_rate_mpd


def _bonus_qualifies(
    bonus: Bonus, merchant_set: set[str], is_fcy: bool
) -> tuple[bool, str | None]:
    """Returns (qualifies, exclusion_reason). exclusion_reason is set when
    a category exclusion vetoes an otherwise-matching bonus."""
    cat_match = (not bonus.categories) or bool(set(bonus.categories) & merchant_set)
    if not cat_match:
        return False, None
    if is_fcy and not bonus.applies_to_fcy:
        return False, None
    if (not is_fcy) and not bonus.applies_to_sgd:
        return False, None
    excl = set(bonus.excluded_categories) & merchant_set
    if excl:
        return False, f"⚠ excluded: {sorted(excl)[0].replace('_', ' ')}"
    return True, None


def _apply_rounding(card: Card, amount_sgd: float) -> tuple[float, str | None]:
    method = card.rounding.method
    if method == "floor_sgd_5":
        amt = float(floor(amount_sgd / 5) * 5)
        if amt < amount_sgd:
            return amt, f"rounded to S${int(amt)}"
        return amt, None
    if method == "floor_sgd_1":
        amt = float(floor(amount_sgd))
        if amt < amount_sgd:
            return amt, f"rounded to S${int(amt)}"
        return amt, None
    return amount_sgd, None


def _format_money(x: float) -> str:
    """S$120 not S$120.0; S$9.99 if cents needed."""
    if x == int(x):
        return f"S${int(x)}"
    return f"S${x:.2f}"


# ---------------------------------------------------------------------------
# v1: caps & min-spend are informational, never deducted
# ---------------------------------------------------------------------------


def _evaluate_card_v1(
    card: Card,
    merchant_set: set[str],
    amount_sgd: float,
    is_fcy: bool,
) -> Recommendation:
    reasons: list[str] = []
    fcy_fee = card.fcy_fee if is_fcy else 0.0
    cost = amount_sgd * (1 + fcy_fee)
    amt_for_miles, rounding_reason = _apply_rounding(card, amount_sgd)
    if rounding_reason:
        reasons.append(rounding_reason)
    base_rate = _base_rate(card, is_fcy)

    best_bonus_rate = 0.0
    best_bonus: Bonus | None = None
    excluded_reason: str | None = None

    for bonus in card.bonus:
        ok, excl = _bonus_qualifies(bonus, merchant_set, is_fcy)
        if not ok:
            if excl is not None and excluded_reason is None:
                excluded_reason = excl
            continue
        if bonus.rate_mpd > best_bonus_rate:
            best_bonus_rate = bonus.rate_mpd
            best_bonus = bonus

    rate = max(base_rate, best_bonus_rate)
    miles = floor(amt_for_miles * rate)
    effective_mpd = round((miles / cost) if cost > 0 else 0.0, 2)

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


# ---------------------------------------------------------------------------
# v2: usage-aware. Min-spend gates bonus; cap blends bonus/base.
# ---------------------------------------------------------------------------


def select_bonus_for_log(
    card: Card,
    merchant_categories: list[str],
    amount_sgd: float,
    is_fcy: bool,
    today: date,
    usage: dict[tuple[str, int], BonusUsage],
    statement_days: dict[str, int] | None = None,
) -> tuple[int | None, str | None, int]:
    """Determine which bonus_idx (if any) a txn qualifies for, and how
    many miles it earns under current usage state.

    Mirrors `_evaluate_card_v2`'s gating logic but tags the qualifying
    bonus by index. Used at log time to record (card, bonus_idx, miles).

    Returns (bonus_idx, bonus_label, miles_earned).
    bonus_idx is None when:
      - no bonus matches at all (falls to base), OR
      - all matching bonuses are gated (min_spend unmet or cap full).
    In that case miles_earned reflects base rate.
    """
    statement_days = statement_days or {}
    merchant_set = set(merchant_categories)
    amt_for_miles, _ = _apply_rounding(card, amount_sgd)
    base_rate = _base_rate(card, is_fcy)
    base_miles = floor(amt_for_miles * base_rate)

    s_day = statement_days.get(card.id)
    candidates: list[tuple[int, int, Bonus]] = []  # (miles, idx, bonus)

    for idx, bonus in enumerate(card.bonus):
        ok, _ = _bonus_qualifies(bonus, merchant_set, is_fcy)
        if not ok:
            continue
        u = usage.get((card.id, idx), BonusUsage(spend_sgd=0.0, min_spend_sgd=0.0))

        if bonus.min_spend_sgd is not None:
            if u.min_spend_sgd + amt_for_miles < bonus.min_spend_sgd:
                continue

        bonus_amt = amt_for_miles
        base_amt_in_blend = 0.0
        if bonus.cap_sgd is not None:
            remaining = max(0.0, bonus.cap_sgd - u.spend_sgd)
            if remaining <= 0:
                continue
            if remaining < amt_for_miles:
                bonus_amt = remaining
                base_amt_in_blend = amt_for_miles - remaining

        miles = floor(bonus_amt * bonus.rate_mpd) + floor(
            base_amt_in_blend * base_rate
        )
        candidates.append((miles, idx, bonus))

    if not candidates:
        return None, None, base_miles

    candidates.sort(key=lambda x: -x[0])
    best_miles, best_idx, best_bonus = candidates[0]
    if best_miles < base_miles:
        return None, None, base_miles
    return best_idx, best_bonus.label, best_miles


def _evaluate_card_v2(
    card: Card,
    merchant_set: set[str],
    amount_sgd: float,
    is_fcy: bool,
    *,
    today: date,
    usage: dict[tuple[str, int], BonusUsage],
    statement_days: dict[str, int],
    posting_delays: dict[str, int] | None = None,
    same_day_merchant: bool = False,
    anniversary_months: dict[str, int] | None = None,
) -> Recommendation:
    reasons: list[str] = []
    fcy_fee = card.fcy_fee if is_fcy else 0.0
    cost = amount_sgd * (1 + fcy_fee)
    amt_for_miles, rounding_reason = _apply_rounding(card, amount_sgd)
    if rounding_reason:
        reasons.append(rounding_reason)
    base_rate = _base_rate(card, is_fcy)
    s_day = statement_days.get(card.id)

    # v3: compute the effective date for period calculations.
    # posting_date cards evaluate caps against when the bank will actually count
    # the spend; transaction_date cards (DBS, Maybank) use today as before.
    if posting_delays is not None and card.tracks_by == "posting_date":
        delay = posting_delays.get(card.id, card.posting_delay_days)
        posting_date = resolve_posting_date(today, delay, same_day_merchant)
        period_date = posting_date
    else:
        posting_date = today
        period_date = today

    ann_month = (anniversary_months or {}).get(card.id)

    candidates: list[tuple[int, Bonus, list[str]]] = []  # (miles, bonus, extras)
    skipped_reasons: list[str] = []
    excluded_reason: str | None = None

    for idx, bonus in enumerate(card.bonus):
        ok, excl = _bonus_qualifies(bonus, merchant_set, is_fcy)
        if not ok:
            if excl is not None and excluded_reason is None:
                excluded_reason = excl
            continue

        u = usage.get((card.id, idx), BonusUsage(spend_sgd=0.0, min_spend_sgd=0.0))
        label = bonus.label or "bonus"

        # Gate: min_spend. The current txn counts toward the threshold; if
        # prior + current still falls short, bonus doesn't fire here.
        if bonus.min_spend_sgd is not None:
            projected = u.min_spend_sgd + amt_for_miles
            if projected < bonus.min_spend_sgd:
                gap = bonus.min_spend_sgd - u.min_spend_sgd
                ms_period = bonus.min_spend_period or "calendar_month"
                n = days_left(ms_period, period_date, s_day, ann_month)
                period_word = ms_period.replace("_", " ")
                day_word = "day" if n == 1 else "days"
                skipped_reasons.append(
                    f"⚠ S${gap:.0f} from {label} min spend, "
                    f"{n} {day_word} left in {period_word}"
                )
                continue

        # Gate: cap. Blend bonus rate on the part that fits, base on overflow.
        bonus_amt = amt_for_miles
        base_amt_in_blend = 0.0
        cap_reason: str | None = None
        if bonus.cap_sgd is not None:
            remaining = max(0.0, bonus.cap_sgd - u.spend_sgd)
            if remaining <= 0:
                skipped_reasons.append(f"⚠ {label} cap reached")
                continue
            if remaining < amt_for_miles:
                bonus_amt = remaining
                base_amt_in_blend = amt_for_miles - remaining
                cap_reason = (
                    f"cap {_format_money(bonus.cap_sgd)} • "
                    f"{_format_money(remaining)} left → "
                    f"{bonus.rate_mpd:g}mpd on {_format_money(bonus_amt)}, "
                    f"{base_rate:g}mpd on {_format_money(base_amt_in_blend)}"
                )

        miles_from_bonus = floor(bonus_amt * bonus.rate_mpd) + floor(
            base_amt_in_blend * base_rate
        )
        extras: list[str] = []
        if cap_reason:
            extras.append(cap_reason)
        candidates.append((miles_from_bonus, bonus, extras))

    base_miles = floor(amt_for_miles * base_rate)

    # Pick the candidate with the highest miles (compare effective miles, not nominal rate).
    if candidates:
        candidates.sort(key=lambda x: -x[0])
        best_miles, best_bonus, extras = candidates[0]
    else:
        best_miles, best_bonus, extras = 0, None, []

    if best_bonus is not None and best_miles >= base_miles:
        label = best_bonus.label or "bonus"
        reasons.insert(0, f"✓ {label}")
        reasons.extend(extras)
        miles = best_miles
    else:
        miles = base_miles
        if excluded_reason is not None:
            reasons.insert(0, excluded_reason)
        # Surface skipped-bonus reasons (min-spend gap, cap-reached) so the
        # user sees why the card fell to base.
        reasons.extend(skipped_reasons)

    if is_fcy:
        reasons.append(f"FCY +{card.fcy_fee * 100:.2f}% fee")

    effective_mpd = round((miles / cost) if cost > 0 else 0.0, 2)

    # v3: generate posting_warning when the spend crosses into a new period.
    rec_posting_warning: str | None = None
    if (
        posting_delays is not None
        and card.tracks_by == "posting_date"
        and posting_date != today
        and best_bonus is not None
        and best_miles >= base_miles
    ):
        cap_period = best_bonus.cap_period or "calendar_month"
        rec_posting_warning = posting_period_warning(
            txn_date=today,
            posting_date=posting_date,
            period=cap_period,
            statement_day=s_day,
            anniversary_month=ann_month,
        )

    return Recommendation(
        card_id=card.id,
        card_name=card.name,
        miles=miles,
        effective_mpd=effective_mpd,
        reasons=reasons,
        posting_warning=rec_posting_warning,
    )
