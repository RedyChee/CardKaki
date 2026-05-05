from pathlib import Path

from cardkaki.data import load_cards, load_merchants

DATA = Path(__file__).resolve().parent.parent / "data"


def test_cards_yaml_validates_and_has_11():
    cards = load_cards(DATA / "cards.yaml")
    assert len(cards) == 11
    expected = {
        "hsbc_revo", "uob_ppv", "uob_vs", "uob_prvi", "uob_lady",
        "citi_rewards", "citi_pm", "dbs_altitude", "dbs_woman",
        "amex_kf", "maybank_xl",
    }
    assert set(cards) == expected


def test_card_invariants():
    cards = load_cards(DATA / "cards.yaml")

    # Sanity: every card has a name and a network
    for cid, c in cards.items():
        assert c.name, f"{cid} missing name"
        assert c.network in {"visa", "mastercard", "amex"}, f"{cid} bad network"

    # FCY-base cards have base_rate_mpd_fcy set
    for cid in ("uob_prvi", "citi_pm", "dbs_altitude", "dbs_woman", "maybank_xl"):
        assert cards[cid].base_rate_mpd_fcy is not None, f"{cid} missing base_rate_mpd_fcy"

    # No-FCY-boost cards leave it unset
    for cid in ("hsbc_revo", "citi_rewards"):
        assert cards[cid].base_rate_mpd_fcy is None, f"{cid} should not set base_rate_mpd_fcy"

    # UOB VS has two bonus rules; DBS Altitude has none
    assert len(cards["uob_vs"].bonus) == 2
    assert cards["dbs_altitude"].bonus == []


def test_merchants_yaml_loads():
    merchants = load_merchants(DATA / "merchants.yaml")
    assert "klook" in merchants
    assert "travel" in merchants["klook"].categories
    assert "travel_excluded" in merchants["klook"].categories
    assert "fast_food_excluded" in merchants["mcdonalds"].categories
    assert "wallet_topup" in merchants["youtrip"].categories


def test_merchants_same_day_posting_flag():
    merchants = load_merchants(DATA / "merchants.yaml")
    assert merchants["grab"].same_day_posting is True
    assert merchants["ntuc"].same_day_posting is True
    assert merchants["shopee"].same_day_posting is False
    assert merchants["klook"].same_day_posting is False
    assert merchants["amazon"].same_day_posting is False
