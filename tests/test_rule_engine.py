from pathlib import Path

import pytest

from cardkaki.data import load_cards
from cardkaki.rule_engine import recommend

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="module")
def cards():
    return load_cards(DATA / "cards.yaml")


# ---------------------------------------------------------------------------
# 4a - 4x: rule engine cases
# ---------------------------------------------------------------------------

def test_4a_empty_wallet(cards):
    assert recommend([], ["groceries"], 50.0) == []


def test_4b_base_rate_only(cards):
    altitude = cards["dbs_altitude"]  # base 1.3, no bonus
    out = recommend([altitude], ["groceries"], 50.0)
    assert out[0].card_id == "dbs_altitude"
    assert out[0].miles == 65  # 50 * 1.3
    assert out[0].effective_mpd == 1.3


def test_4c_bonus_matches(cards):
    revo = cards["hsbc_revo"]
    out = recommend([revo], ["groceries", "contactless"], 50.0)
    r = out[0]
    assert r.card_id == "hsbc_revo"
    assert r.miles == 200  # floor_sgd_1: 50 * 4
    assert r.effective_mpd == 4.0
    assert any(reason.startswith("✓") for reason in r.reasons)


def test_4d_exclusion_vetoes_bonus(cards):
    revo = cards["hsbc_revo"]
    out = recommend([revo], ["travel", "travel_excluded", "online_travel"], 320.0)
    r = out[0]
    assert r.miles == 128  # base 0.4 * 320
    assert r.effective_mpd == 0.4
    assert any("excluded" in reason.lower() for reason in r.reasons)


def test_4e_ranking_by_miles(cards):
    wallet = [cards["citi_pm"], cards["hsbc_revo"], cards["uob_ppv"]]
    out = recommend(wallet, ["online_shopping"], 50.0)
    # HSBC Revo (4mpd) and UOB PPV (4mpd online) both win at 200 mi
    # alphabetic break: HSBC Revolution < UOB Preferred Platinum Visa
    assert out[0].card_id == "hsbc_revo"
    assert out[0].miles == 200
    assert out[1].card_id == "uob_ppv"
    assert out[1].miles == 200
    assert out[2].card_id == "citi_pm"
    assert out[2].miles == 60  # 50 * 1.2


def test_4f_uob_floor_sgd_5_kills_small_txn(cards):
    ppv = cards["uob_ppv"]
    out = recommend([ppv], ["online_shopping"], 9.99)
    r = out[0]
    assert r.miles == 20  # floor(9.99/5)*5 * 4 = 5*4
    assert r.effective_mpd == 2.0
    assert any("rounded" in reason.lower() for reason in r.reasons)


def test_4g_bonus_skipped_on_fcy_when_not_supported(cards):
    # UOB PPV bonus rules don't have applies_to_fcy=true → FCY falls to base
    ppv = cards["uob_ppv"]
    out = recommend([ppv], ["online_shopping"], 100.0, is_fcy=True)
    r = out[0]
    # base 0.4 with floor_sgd_5: 100 * 0.4 = 40 mi
    assert r.miles == 40
    assert any("FCY" in reason for reason in r.reasons)


def test_4h_bonus_applies_on_fcy_when_supported(cards):
    revo = cards["hsbc_revo"]
    # HSBC Revo bonus has applies_to_fcy=true; cold_storage qualifies (groceries → wait,
    # groceries isn't in revo bonus categories. Use online_shopping).
    out = recommend([revo], ["online_shopping"], 100.0, is_fcy=True)
    r = out[0]
    # floor_sgd_1: 100 → 100, bonus 4mpd → 400 mi
    assert r.miles == 400
    # cost = 100 * 1.0325 = 103.25; effective_mpd = 400/103.25 ≈ 3.87
    assert abs(r.effective_mpd - 3.87) < 0.02


def test_4i_effective_mpd_reflects_fcy_cost(cards):
    prvi = cards["uob_prvi"]
    out = recommend([prvi], ["online_shopping"], 100.0, is_fcy=True)
    r = out[0]
    # PRVI FCY base 2.4; floor_sgd_5: 100 → 100 → 240 mi
    # cost = 103.25; eff = 240/103.25 ≈ 2.32
    assert r.miles == 240
    assert abs(r.effective_mpd - 2.32) < 0.02


def test_4j_multiple_bonus_rules_pick_max(cards):
    # PRVI has Agoda 8mpd and base 1.4. Agoda txn → bonus wins.
    prvi = cards["uob_prvi"]
    out = recommend([prvi], ["agoda", "travel", "travel_excluded", "online_travel"], 200.0)
    r = out[0]
    assert r.miles == 1600  # 200 * 8
    assert r.effective_mpd == 8.0


def test_4k_min_spend_surfaced_in_reasons(cards):
    vs = cards["uob_vs"]
    out = recommend([vs], ["petrol", "contactless"], 80.0)
    r = out[0]
    assert any("min spend" in reason.lower() for reason in r.reasons)


def test_4l_no_rounding_for_per_dollar_card(cards):
    # Citi Rewards uses floor_sgd_1, so 9.99 rounds to 9 (not 5).
    rewards = cards["citi_rewards"]
    out = recommend([rewards], ["online_shopping"], 9.99)
    r = out[0]
    assert r.miles == 36  # 9 * 4
    assert r.effective_mpd == round(36 / 9.99, 2)


def test_4m_uob_lady_chosen_category(cards):
    lady = cards["uob_lady"]
    # Without lady_chosen → falls to base
    out_base = recommend([lady], ["online_shopping"], 100.0)
    assert out_base[0].miles == 40  # base 0.4 with floor_sgd_5: 100 * 0.4
    # With lady_chosen → 4mpd bonus fires
    out_bonus = recommend([lady], ["lady_chosen", "online_shopping"], 100.0)
    assert out_bonus[0].miles == 400


def test_4n_one_rec_per_card(cards):
    revo = cards["hsbc_revo"]
    out = recommend([revo, revo], ["groceries"], 50.0)
    # Two cards in (even if same instance) → two recs. Engine doesn't dedupe; caller responsibility.
    assert len(out) == 2
    # But recommend([revo]) returns exactly one rec.
    assert len(recommend([revo], ["groceries"], 50.0)) == 1


def test_4o_dbs_woman_online(cards):
    woman = cards["dbs_woman"]
    out = recommend([woman], ["online_shopping"], 50.0)
    r = out[0]
    assert r.miles == 200  # floor_sgd_5: 50 * 4
    # GrabPay top-up vetoes
    out_veto = recommend([woman], ["online_shopping", "wallet_topup"], 50.0)
    assert out_veto[0].miles == 20  # base 0.4 * 50


def test_4p_amex_grab(cards):
    amex = cards["amex_kf"]
    out = recommend([amex], ["transport", "online_shopping", "grab"], 15.0)
    r = out[0]
    # 2 mpd bonus on grab; 15 * 2 = 30
    assert r.miles == 30
    assert r.effective_mpd == 2.0


def test_4q_maybank_xl_klook_travel_bonus(cards):
    maybank = cards["maybank_xl"]
    out = recommend([maybank], ["travel", "travel_excluded", "online_travel"], 320.0)
    r = out[0]
    # 4mpd applies (Maybank doesn't have travel_excluded in its excluded list)
    assert r.miles == 1280  # 320 * 4
    assert r.effective_mpd == 4.0
    # Min-spend reason surfaces
    assert any("min spend" in reason.lower() for reason in r.reasons)


def test_4r_maybank_xl_fcy_base_4mpd(cards):
    maybank = cards["maybank_xl"]
    # Generic FCY transaction with no bonus category match still earns 4mpd via FCY base
    out = recommend([maybank], ["unknown_category"], 100.0, is_fcy=True)
    r = out[0]
    # base FCY 4.0; floor_sgd_5: 100 → 100 → 400 mi
    assert r.miles == 400


def test_4s_dbs_woman_fcy_online(cards):
    woman = cards["dbs_woman"]
    out = recommend([woman], ["online_shopping"], 200.0, is_fcy=True)
    r = out[0]
    # 4mpd bonus applies on FCY-online; 200 * 4 = 800
    assert r.miles == 800


def test_4t_dbs_woman_floor_sgd_5(cards):
    woman = cards["dbs_woman"]
    out = recommend([woman], ["online_shopping"], 9.99)
    r = out[0]
    assert r.miles == 20  # floor(9.99/5)*5 * 4


def test_4u_dbs_woman_exclusion(cards):
    woman = cards["dbs_woman"]
    out = recommend([woman], ["online_shopping", "wallet_topup"], 50.0)
    r = out[0]
    # bonus vetoed → falls to base 0.4 with floor_sgd_5: 50 * 0.4 = 20
    assert r.miles == 20


def test_4v_zero_amount_raises(cards):
    revo = cards["hsbc_revo"]
    with pytest.raises(ValueError):
        recommend([revo], ["groceries"], 0)


def test_4w_unknown_category_falls_to_base(cards):
    revo = cards["hsbc_revo"]
    out = recommend([revo], ["something_random"], 50.0)
    r = out[0]
    assert r.miles == 20  # base 0.4 * 50


def test_4x_effective_mpd_two_dp(cards):
    prvi = cards["uob_prvi"]
    out = recommend([prvi], ["unknown"], 9.99)
    r = out[0]
    # 9.99 with floor_sgd_5 → 5; 5 * 1.4 = 7 mi; eff = 7/9.99 = 0.7007... → 0.70
    assert r.miles == 7
    assert r.effective_mpd == 0.70


# ---------------------------------------------------------------------------
# Extra cases
# ---------------------------------------------------------------------------

def test_uob_vs_overseas_wildcard_bonus(cards):
    """UOB VS bonus rule with empty categories should match any FCY transaction."""
    vs = cards["uob_vs"]
    out = recommend([vs], ["unknown_random_merchant"], 100.0, is_fcy=True)
    r = out[0]
    # 4mpd applies; floor_sgd_5: 100 → 400 mi
    assert r.miles == 400


def test_dbs_altitude_no_bonus_anywhere(cards):
    altitude = cards["dbs_altitude"]
    # No bonus on online, dining, anywhere
    out = recommend([altitude], ["online_shopping", "dining_local"], 100.0)
    r = out[0]
    assert r.miles == 130  # 100 * 1.3
    assert r.effective_mpd == 1.3
    # FCY base 2.2
    out_fcy = recommend([altitude], ["unknown"], 100.0, is_fcy=True)
    assert out_fcy[0].miles == 220


def test_amex_kf_no_fcy_boost(cards):
    amex = cards["amex_kf"]
    out = recommend([amex], ["unknown"], 100.0, is_fcy=True)
    # Base 1.1 SGD = base 1.1 FCY for Amex KF
    assert out[0].miles == 110


def test_citi_pm_base_only_after_promo_lapse(cards):
    """Citi PM's Kaligo/Agoda promos lapsed 31 Dec 2025 — card is base-only now."""
    pm = cards["citi_pm"]
    out_sgd = recommend([pm], ["agoda"], 100.0, is_fcy=False)
    out_fcy = recommend([pm], ["agoda"], 100.0, is_fcy=True)
    assert out_sgd[0].miles == 120  # 100 * 1.2 base SGD
    assert out_fcy[0].miles == 220  # 100 * 2.2 base FCY (cost 103.25)


def test_hsbc_revo_default_tier_is_regular(cards):
    """No card_tiers passed → defaults to regular tier (4mpd cap S$1k)."""
    revo = cards["hsbc_revo"]
    out = recommend([revo], ["online_shopping"], 100.0)
    assert out[0].miles == 400  # 100 * 4 mpd regular


def test_hsbc_revo_enhanced_tier_8mpd(cards):
    """Enhanced-tier user (S$50K+ EGA ADB) gets 8mpd."""
    revo = cards["hsbc_revo"]
    out = recommend(
        [revo], ["online_shopping"], 100.0,
        card_tiers={"hsbc_revo": "enhanced"},
    )
    assert out[0].miles == 800  # 100 * 8 mpd enhanced


def test_hsbc_revo_explicit_regular_tier(cards):
    revo = cards["hsbc_revo"]
    out = recommend(
        [revo], ["online_shopping"], 100.0,
        card_tiers={"hsbc_revo": "regular"},
    )
    assert out[0].miles == 400


def test_hsbc_revo_enhanced_cap_blends_at_1200(cards):
    """Enhanced tier cap is S$1,200; cap-blend kicks in past that."""
    revo = cards["hsbc_revo"]
    usage = {("hsbc_revo", 1): BonusUsage(spend_sgd=1100, min_spend_sgd=0)}
    out = recommend(
        [revo], ["online_shopping"], 200.0,
        today=date(2026, 5, 4), usage=usage,
        card_tiers={"hsbc_revo": "enhanced"},
    )
    # 100 left of S$1,200 cap: 100 @ 8mpd = 800; 100 base @ 0.4 = 40 → 840 mi
    assert out[0].miles == 800 + 40


def test_citi_rewards_fcy_no_bonus(cards):
    """Citi Rewards bonus is SGD-only — FCY earns base (corrected per Feb 2026 review)."""
    rewards = cards["citi_rewards"]
    out = recommend([rewards], ["online_shopping"], 100.0, is_fcy=True)
    # Base 0.4 only; floor_sgd_1(100)=100; 100*0.4=40
    assert out[0].miles == 40


def test_uob_prvi_seasia_fcy_3mpd(cards):
    """SE-Asia FCY rule fires when seasia_fcy category is injected (e.g., IDR txn)."""
    prvi = cards["uob_prvi"]
    out = recommend([prvi], ["seasia_fcy"], 100.0, is_fcy=True)
    # 3 mpd; floor_sgd_5(100)=100; 100*3=300
    assert out[0].miles == 300


def test_uob_prvi_fcy_without_seasia_falls_to_base(cards):
    """Generic FCY without seasia_fcy category gets the 2.4 base FCY rate."""
    prvi = cards["uob_prvi"]
    out = recommend([prvi], ["unknown"], 100.0, is_fcy=True)
    # base FCY 2.4; floor_sgd_5(100)=100; 100*2.4=240
    assert out[0].miles == 240


def test_uob_prvi_no_more_expedia_bonus(cards):
    """Expedia categories should fall to base now that the bonus has lapsed (31 Mar 2026)."""
    prvi = cards["uob_prvi"]
    out = recommend([prvi], ["expedia"], 100.0)
    # base 1.4; floor_sgd_5(100)=100; 100*1.4=140
    assert out[0].miles == 140
    # Same for expedia_flight (the 3mpd rule was removed too)
    out_flight = recommend([prvi], ["expedia_flight"], 100.0)
    assert out_flight[0].miles == 140


def test_uob_lady_solitaire_loads_and_recommends(cards):
    sol = cards["uob_lady_solitaire"]
    out = recommend([sol], ["lady_chosen", "online_shopping"], 100.0)
    # 4 mpd; floor_sgd_5(100)=100; 100*4=400
    assert out[0].miles == 400


def test_uob_lady_solitaire_metal_higher_cap(cards):
    """Solitaire Metal cap is S$2,000 vs Lady's S$1,000."""
    metal = cards["uob_lady_solitaire_metal"]
    assert metal.bonus[0].cap_sgd == 2000


def test_hsbc_revo_fast_food_excluded(cards):
    revo = cards["hsbc_revo"]
    out = recommend([revo], ["dining_local", "contactless", "fast_food_excluded"], 15.0)
    r = out[0]
    # bonus excluded → base 0.4
    assert r.miles == 6  # 15 * 0.4


# ---------------------------------------------------------------------------
# v2: usage-aware cap & min-spend gating
# ---------------------------------------------------------------------------

from datetime import date

from cardkaki.models import BonusUsage


def test_no_usage_dict_behaves_as_v1(cards):
    """All v1 tests above use usage=None implicitly; this asserts the
    explicit None path returns the same result as the default path."""
    revo = cards["hsbc_revo"]
    out_default = recommend([revo], ["online_shopping"], 50.0)
    out_none = recommend([revo], ["online_shopping"], 50.0, usage=None)
    assert out_default[0].model_dump() == out_none[0].model_dump()


def test_cap_exhausted_falls_to_base(cards):
    ppv = cards["uob_ppv"]
    # bonus_idx 0 = mobile contactless, S$600 cap. Filling it completely.
    usage = {
        ("uob_ppv", 0): BonusUsage(spend_sgd=600, min_spend_sgd=0),
        ("uob_ppv", 1): BonusUsage(spend_sgd=0, min_spend_sgd=0),
    }
    out = recommend(
        [ppv], ["mobile_contactless"], 50.0,
        today=date(2026, 5, 4), usage=usage,
    )
    r = out[0]
    # 50 * floor_sgd_5 = 50; base 0.4 → 20 mi
    assert r.miles == 20
    assert any("cap reached" in reason.lower() for reason in r.reasons)


def test_cap_blended_bonus_plus_base_on_overflow(cards):
    ppv = cards["uob_ppv"]
    # 520 used; 80 remaining of S$600 cap. 200 SGD txn:
    #   bonus on 80 (4mpd) → 320 mi
    #   base on 120 (0.4mpd, floor_sgd_5: 120) → 48 mi
    #   total: 368 mi
    usage = {
        ("uob_ppv", 0): BonusUsage(spend_sgd=520, min_spend_sgd=0),
        ("uob_ppv", 1): BonusUsage(spend_sgd=0, min_spend_sgd=0),
    }
    out = recommend(
        [ppv], ["mobile_contactless"], 200.0,
        today=date(2026, 5, 4), usage=usage,
    )
    r = out[0]
    assert r.miles == 320 + 48
    assert any("S$80 left" in reason for reason in r.reasons)


def test_min_spend_unmet_skips_bonus(cards):
    vs = cards["uob_vs"]
    # bonus_idx 1 = petrol+contactless, S$1000 min_spend. Usage: 0.
    # Txn 80 still under threshold (0 + 80 < 1000), bonus doesn't fire.
    usage = {
        ("uob_vs", 0): BonusUsage(spend_sgd=0, min_spend_sgd=0),
        ("uob_vs", 1): BonusUsage(spend_sgd=0, min_spend_sgd=0),
    }
    out = recommend(
        [vs], ["petrol", "contactless"], 80.0,
        today=date(2026, 5, 4), usage=usage,
    )
    r = out[0]
    # base 0.4 with floor_sgd_5(80)=80 → 32 mi
    assert r.miles == 32
    assert any("from" in reason and "min spend" in reason for reason in r.reasons)


def test_min_spend_met_unlocks_bonus(cards):
    vs = cards["uob_vs"]
    # min_spend met: prior 1100. Txn 100 → bonus rate fires.
    usage = {
        ("uob_vs", 0): BonusUsage(spend_sgd=0, min_spend_sgd=0),
        ("uob_vs", 1): BonusUsage(spend_sgd=0, min_spend_sgd=1100),
    }
    out = recommend(
        [vs], ["petrol", "contactless"], 100.0,
        today=date(2026, 5, 4), usage=usage,
    )
    r = out[0]
    # 100 * 4 = 400
    assert r.miles == 400


def test_min_spend_progress_reason_includes_days_left(cards):
    vs = cards["uob_vs"]
    # 920 prior; this 80 still leaves projected at 1000. Edge: projected==min_spend should fire.
    usage = {
        ("uob_vs", 0): BonusUsage(spend_sgd=0, min_spend_sgd=0),
        ("uob_vs", 1): BonusUsage(spend_sgd=0, min_spend_sgd=900),  # 900+80=980<1000
    }
    out = recommend(
        [vs], ["petrol", "contactless"], 80.0,
        today=date(2026, 5, 16),
        usage=usage,
        statement_days={"uob_vs": 22},
    )
    r = out[0]
    # base 0.4 * 80 = 32
    assert r.miles == 32
    msg = " ".join(r.reasons)
    assert "S$100" in msg  # gap = 1000 - 900
    assert "left" in msg.lower()


def test_period_boundary_calendar_month_resets_cap(cards):
    """If today is May 1 and cap usage entries reflect April spend, the
    engine still sees the May usage. This test simulates by passing
    explicit zero usage for May while May 1 is `today`."""
    ppv = cards["uob_ppv"]
    usage = {
        ("uob_ppv", 0): BonusUsage(spend_sgd=0, min_spend_sgd=0),
        ("uob_ppv", 1): BonusUsage(spend_sgd=0, min_spend_sgd=0),
    }
    out = recommend(
        [ppv], ["mobile_contactless"], 50.0,
        today=date(2026, 5, 1), usage=usage,
    )
    # Full bonus: 50 * 4 = 200
    assert out[0].miles == 200


def test_min_spend_for_one_bonus_doesnt_count_other(cards):
    vs = cards["uob_vs"]
    # Bonus 0 = FCY (S$1000 min); bonus 1 = petrol+contactless (S$1000 min).
    # Filling FCY min should NOT unlock the petrol+contactless bonus.
    usage = {
        ("uob_vs", 0): BonusUsage(spend_sgd=0, min_spend_sgd=2000),
        ("uob_vs", 1): BonusUsage(spend_sgd=0, min_spend_sgd=0),
    }
    out = recommend(
        [vs], ["petrol", "contactless"], 80.0,
        today=date(2026, 5, 4), usage=usage,
    )
    r = out[0]
    # base 0.4 * 80 = 32
    assert r.miles == 32


def test_cap_with_floor_sgd_5_rounding_interaction(cards):
    # UOB PPV: bonus_idx 0, cap 600, used 595 → 5 remaining.
    # Txn 9.99 → floor_sgd_5 = 5. bonus_amt=min(5,5)=5, base=0. Miles = 5*4 = 20.
    ppv = cards["uob_ppv"]
    usage = {
        ("uob_ppv", 0): BonusUsage(spend_sgd=595, min_spend_sgd=0),
        ("uob_ppv", 1): BonusUsage(spend_sgd=0, min_spend_sgd=0),
    }
    out = recommend(
        [ppv], ["mobile_contactless"], 9.99,
        today=date(2026, 5, 4), usage=usage,
    )
    assert out[0].miles == 20  # full 5 still fits in remaining 5


def test_cap_blended_picks_higher_effective_miles_bonus(cards):
    """If two bonuses qualify and one is cap-blended down, the engine should
    pick the one that yields more miles for THIS txn — not by nominal rate alone.
    Uses UOB VS where both bonus rules can fire on a contactless FCY txn:
    rule 0 (overseas/FCY 4mpd) qualifies, rule 1 (petrol+contactless 4mpd) is
    SGD-only so rejected on FCY. The single qualifying rule wins.
    """
    vs = cards["uob_vs"]
    # FCY contactless txn, prior FCY min-spend already met (1100), petrol min unmet.
    out = recommend(
        [vs], ["contactless"], 100.0, is_fcy=True,
        today=date(2026, 5, 4),
        usage={
            ("uob_vs", 0): BonusUsage(spend_sgd=0, min_spend_sgd=1100),
            ("uob_vs", 1): BonusUsage(spend_sgd=0, min_spend_sgd=0),
        },
    )
    # FCY 4mpd fires; floor_sgd_5(100)=100; 100*4=400
    assert out[0].miles == 400


def test_lady_chosen_with_cap_partial(cards):
    lady = cards["uob_lady"]
    # cap 1000; 950 used; txn 100 → 50 bonus, 50 base.
    # bonus: 50 * 4 = 200; base: floor_sgd_5(100)=100, but blend uses 50 base → floor(50 * 0.4) = 20
    usage = {("uob_lady", 0): BonusUsage(spend_sgd=950, min_spend_sgd=0)}
    out = recommend(
        [lady], ["lady_chosen", "online_shopping"], 100.0,
        today=date(2026, 5, 4), usage=usage,
    )
    r = out[0]
    assert r.miles == 200 + 20


# ---------------------------------------------------------------------------
# v3: posting date intelligence
# ---------------------------------------------------------------------------


def test_v3_posting_warning_on_period_boundary(cards):
    """HSBC Revo T+1, txn Apr 30 → posts May 1 — should carry posting_warning."""
    revo = cards["hsbc_revo"]
    usage = {("hsbc_revo", 0): BonusUsage(spend_sgd=0, min_spend_sgd=0)}
    out = recommend(
        [revo], ["groceries", "contactless"], 45.0,
        today=date(2026, 4, 30),
        usage=usage,
        posting_delays={"hsbc_revo": 1},
        same_day_merchant=False,
    )
    r = out[0]
    assert r.posting_warning is not None
    assert "May" in r.posting_warning
    assert "Apr" in r.posting_warning


def test_v3_no_posting_warning_within_period(cards):
    """HSBC Revo T+1, txn Apr 28 → posts Apr 29 — same period, no warning."""
    revo = cards["hsbc_revo"]
    usage = {("hsbc_revo", 0): BonusUsage(spend_sgd=0, min_spend_sgd=0)}
    out = recommend(
        [revo], ["groceries", "contactless"], 45.0,
        today=date(2026, 4, 28),
        usage=usage,
        posting_delays={"hsbc_revo": 1},
        same_day_merchant=False,
    )
    r = out[0]
    assert r.posting_warning is None


def test_v3_same_day_merchant_no_warning_on_boundary(cards):
    """Same-day merchant overrides delay; no warning even on Apr 30."""
    revo = cards["hsbc_revo"]
    usage = {("hsbc_revo", 0): BonusUsage(spend_sgd=0, min_spend_sgd=0)}
    out = recommend(
        [revo], ["groceries", "contactless"], 45.0,
        today=date(2026, 4, 30),
        usage=usage,
        posting_delays={"hsbc_revo": 1},
        same_day_merchant=True,
    )
    r = out[0]
    assert r.posting_warning is None


def test_v3_transaction_date_card_never_gets_warning(cards):
    """DBS Altitude tracks by transaction_date — no posting warning regardless."""
    altitude = cards["dbs_altitude"]
    out = recommend(
        [altitude], ["groceries"], 45.0,
        today=date(2026, 4, 30),
        usage={},
        posting_delays={"dbs_altitude": 1},
        same_day_merchant=False,
    )
    r = out[0]
    assert r.posting_warning is None


def test_v3_posting_none_when_no_delays_passed(cards):
    """When posting_delays not passed (v2 callers), no posting_warning."""
    revo = cards["hsbc_revo"]
    usage = {("hsbc_revo", 0): BonusUsage(spend_sgd=0, min_spend_sgd=0)}
    out = recommend(
        [revo], ["groceries", "contactless"], 45.0,
        today=date(2026, 4, 30),
        usage=usage,
    )
    r = out[0]
    assert r.posting_warning is None


def test_v3_cap_period_evaluated_using_posting_date(cards):
    """For a posting_date card near cap reset, the engine uses posting_date's
    period — April cap may be full but posting lands in May (fresh cap)."""
    from cardkaki.usage import build_usage
    from cardkaki.models import TxnRow
    from datetime import datetime

    revo = cards["hsbc_revo"]
    # Simulate April usage: 1000 SGD posted in April (txn_date all in April,
    # and they post in April too since it's before Apr 30).
    apr_txns = [
        TxnRow(
            tx_id=f"t{i}", telegram_user_id=1, card_id="hsbc_revo",
            bonus_idx=0, bonus_label="online or contactless",
            merchant="shopee", amount_sgd=100.0, is_fcy=False,
            miles_earned=400, txn_date=date(2026, 4, i + 1),
            created_at=datetime(2026, 4, i + 1),
        )
        for i in range(10)  # 10 * 100 = 1000 SGD in April
    ]
    # Build usage using posting_date = May 1 as the reference (transaction on
    # Apr 30 posts May 1, so we want May's cap window).
    usage = build_usage(
        apr_txns, [revo],
        today=date(2026, 5, 1),  # period_date = May 1 (posting date)
        statement_days={},
        posting_delays={"hsbc_revo": 1},
    )
    # April transactions post in April, not May → May usage should be 0
    assert usage[("hsbc_revo", 0)].spend_sgd == 0.0

    out = recommend(
        [revo], ["groceries", "contactless"], 100.0,
        today=date(2026, 4, 30),
        usage=usage,
        posting_delays={"hsbc_revo": 1},
        same_day_merchant=False,
    )
    r = out[0]
    # May cap is fresh → full bonus rate 4.0 mpd on 100 SGD (floor_sgd_1)
    assert r.miles == 400
    assert r.posting_warning is not None
