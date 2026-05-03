"""Ship-gate regression tests. Each scenario in fixtures/scenarios.yaml
exercises an end-to-end pipeline: parse() → merchants lookup → recommend(),
and pins the top card.
"""
from pathlib import Path

import pytest
import yaml

from cardkaki.data import load_cards, load_merchants
from cardkaki.parser import parse
from cardkaki.rule_engine import recommend

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = yaml.safe_load((Path(__file__).parent / "fixtures" / "scenarios.yaml").read_text())


@pytest.fixture(scope="module")
def catalog():
    return load_cards(ROOT / "data" / "cards.yaml")


@pytest.fixture(scope="module")
def merchants():
    return load_merchants(ROOT / "data" / "merchants.yaml")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["id"])
def test_scenario(scenario, catalog, merchants):
    parsed = parse(scenario["text"])
    wallet = [catalog[cid] for cid in scenario["wallet"]]
    categories = merchants.get(parsed.merchant, [])

    recs = recommend(
        wallet, categories, parsed.amount_sgd, is_fcy=parsed.is_fcy
    )

    assert recs, f"{scenario['id']}: empty recommendations"
    assert recs[0].card_id == scenario["expected_top"], (
        f"{scenario['id']}: top was {recs[0].card_id} (miles={recs[0].miles}); "
        f"expected {scenario['expected_top']}. "
        f"All recs: {[(r.card_id, r.miles) for r in recs]}"
    )
