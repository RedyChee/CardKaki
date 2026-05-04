"""Ship-gate regression tests. Each scenario in fixtures/scenarios.yaml
exercises an end-to-end pipeline: parse() → merchants lookup → recommend(),
and pins the top card.

v2 scenarios optionally include `seed_txns:` and `today:` to exercise
cap-aware ranking via the materialized usage state.
"""
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

from cardkaki.data import load_cards, load_merchants
from cardkaki.models import TxnRow
from cardkaki.parser import parse
from cardkaki.rule_engine import recommend
from cardkaki.usage import build_usage

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = yaml.safe_load((Path(__file__).parent / "fixtures" / "scenarios.yaml").read_text())


@pytest.fixture(scope="module")
def catalog():
    return load_cards(ROOT / "data" / "cards.yaml")


@pytest.fixture(scope="module")
def merchants():
    return load_merchants(ROOT / "data" / "merchants.yaml")


def _build_seed_txns(seed: list[dict]) -> list[TxnRow]:
    out = []
    for i, s in enumerate(seed):
        out.append(TxnRow(
            tx_id=f"seed-{i}",
            telegram_user_id=1,
            card_id=s["card"],
            bonus_idx=s.get("bonus_idx"),
            bonus_label=s.get("label"),
            merchant=s["merchant"],
            amount_sgd=float(s["amount"]),
            is_fcy=s.get("fcy", False),
            miles_earned=0,
            txn_date=date.fromisoformat(s["date"]) if "date" in s else date(2026, 5, 1),
            created_at=datetime(2026, 5, 1, 12, 0, 0),
        ))
    return out


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["id"])
def test_scenario(scenario, catalog, merchants):
    parsed = parse(scenario["text"])
    wallet = [catalog[cid] for cid in scenario["wallet"]]
    entry = merchants.get(parsed.merchant)
    categories = entry.categories if entry is not None else []
    today = date.fromisoformat(scenario["today"]) if "today" in scenario else date.today()

    seed_txns = _build_seed_txns(scenario.get("seed_txns", []))
    usage = build_usage(seed_txns, wallet, today) if seed_txns else None

    recs = recommend(
        wallet,
        categories,
        parsed.amount_sgd,
        is_fcy=parsed.is_fcy,
        today=today,
        usage=usage,
    )

    assert recs, f"{scenario['id']}: empty recommendations"
    assert recs[0].card_id == scenario["expected_top"], (
        f"{scenario['id']}: top was {recs[0].card_id} (miles={recs[0].miles}); "
        f"expected {scenario['expected_top']}. "
        f"All recs: {[(r.card_id, r.miles) for r in recs]}"
    )
