from datetime import date
from pathlib import Path

import pytest

from cardkaki.bot import (
    compute_recommendation_payload,
    format_anniversary_prompt,
    format_card_list,
    format_catalog_keyboard,
    format_log_buttons,
    format_menu_keyboard,
    format_recommendations,
    format_statement_day_prompt,
    format_wallet_keyboard,
    handle_cards_command,
    handle_lady_choice_command,
    handle_log_command,
    handle_pools_command,
    handle_recent_command,
    handle_text_message,
)
from cardkaki.data import load_cards, load_merchants
from cardkaki.models import Recommendation
from cardkaki.storage import Storage

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="module")
def catalog():
    return load_cards(DATA / "cards.yaml")


@pytest.fixture(scope="module")
def merchants():
    return load_merchants(DATA / "merchants.yaml")


@pytest.fixture
async def storage(tmp_path):
    s = Storage(tmp_path / "users.sqlite")
    await s.init()
    return s


# ---------------------------------------------------------------------------
# /cards handlers
# ---------------------------------------------------------------------------

async def test_cards_catalog_lists_all_ids(storage, catalog):
    reply = await handle_cards_command(["catalog"], 1, storage, catalog)
    for cid in catalog:
        assert cid in reply


async def test_cards_catalog_no_usage_instructions(storage, catalog):
    reply = await handle_cards_command(["catalog"], 1, storage, catalog)
    assert "Usage:" not in reply


async def test_cards_list_empty(storage, catalog):
    reply = await handle_cards_command(["list"], 1, storage, catalog)
    assert "haven't added" in reply.lower()


async def test_cards_default_to_list(storage, catalog):
    reply = await handle_cards_command([], 1, storage, catalog)
    assert "haven't added" in reply.lower()


async def test_cards_add_valid(storage, catalog):
    reply = await handle_cards_command(["add", "hsbc_revo"], 1, storage, catalog)
    assert "added" in reply.lower()
    assert "HSBC Revolution" in reply


async def test_cards_add_duplicate(storage, catalog):
    await handle_cards_command(["add", "hsbc_revo"], 1, storage, catalog)
    reply = await handle_cards_command(["add", "hsbc_revo"], 1, storage, catalog)
    assert "Already" in reply


async def test_cards_add_unknown(storage, catalog):
    reply = await handle_cards_command(["add", "bogus_card"], 1, storage, catalog)
    assert "Unknown card id" in reply
    assert "hsbc_revo" in reply  # lists valid ids


async def test_cards_list_after_add(storage, catalog):
    await handle_cards_command(["add", "hsbc_revo"], 1, storage, catalog)
    await handle_cards_command(["add", "uob_ppv"], 1, storage, catalog)
    reply = await handle_cards_command(["list"], 1, storage, catalog)
    assert "HSBC Revolution" in reply
    assert "UOB Preferred Platinum Visa" in reply


async def test_cards_remove(storage, catalog):
    await handle_cards_command(["add", "hsbc_revo"], 1, storage, catalog)
    reply = await handle_cards_command(["remove", "hsbc_revo"], 1, storage, catalog)
    assert "removed" in reply.lower()
    reply2 = await handle_cards_command(["remove", "hsbc_revo"], 1, storage, catalog)
    assert "wasn't in" in reply2


# ---------------------------------------------------------------------------
# Text → recommendation pipeline
# ---------------------------------------------------------------------------

async def test_text_empty_wallet(storage, catalog, merchants):
    reply = await handle_text_message("cold storage 45", 1, storage, catalog, merchants)
    assert "wallet is empty" in reply.lower()


async def test_text_recommendation(storage, catalog, merchants):
    await handle_cards_command(["add", "hsbc_revo"], 1, storage, catalog)
    reply = await handle_text_message("cold storage 45", 1, storage, catalog, merchants)
    assert "HSBC Revolution" in reply
    assert "4.0 mpd" in reply


async def test_text_klook_fcy_routes_correctly(storage, catalog, merchants):
    # Maybank XL is the optimal card on klook (4mpd via travel)
    await handle_cards_command(["add", "hsbc_revo"], 1, storage, catalog)
    await handle_cards_command(["add", "uob_prvi"], 1, storage, catalog)
    await handle_cards_command(["add", "maybank_xl"], 1, storage, catalog)
    reply = await handle_text_message("klook 320 fcy", 1, storage, catalog, merchants)
    # First medal goes to Maybank XL because 320 * 4 = 1280 mi
    assert "Maybank XL Rewards" in reply
    assert "🥇" in reply
    # Confirm Maybank appears before HSBC in the reply
    maybank_idx = reply.index("Maybank XL Rewards")
    if "HSBC Revolution" in reply:
        hsbc_idx = reply.index("HSBC Revolution")
        assert maybank_idx < hsbc_idx


async def test_text_garbled_input(storage, catalog, merchants):
    await handle_cards_command(["add", "hsbc_revo"], 1, storage, catalog)
    reply = await handle_text_message("???", 1, storage, catalog, merchants)
    assert "couldn't parse" in reply.lower()


async def test_text_unknown_merchant_warns(storage, catalog, merchants):
    await handle_cards_command(["add", "hsbc_revo"], 1, storage, catalog)
    reply = await handle_text_message("acmecorp 50", 1, storage, catalog, merchants)
    assert "unknown merchant" in reply.lower()


async def test_text_all_excluded_warning(storage, catalog, merchants):
    # Klook is excluded by HSBC Revo only — falls back to base, all_excluded triggers
    await handle_cards_command(["add", "hsbc_revo"], 1, storage, catalog)
    reply = await handle_text_message("klook 100", 1, storage, catalog, merchants)
    assert "falling back to base" in reply.lower()


# ---------------------------------------------------------------------------
# format_recommendations / format_card_list snapshots
# ---------------------------------------------------------------------------

def test_format_recs_empty():
    out = format_recommendations(
        [], merchant="x", amount_sgd=1, is_fcy=False
    )
    assert "wallet is empty" in out.lower()


def test_format_recs_single():
    rec = Recommendation(
        card_id="hsbc_revo", card_name="HSBC Revolution",
        miles=180, effective_mpd=4.0, reasons=["✓ online or contactless"],
    )
    out = format_recommendations(
        [rec], merchant="cold_storage", amount_sgd=45, is_fcy=False
    )
    assert "🥇" in out
    assert "HSBC Revolution" in out
    assert "180 mi" not in out
    assert "4.0 mpd" in out


def test_format_recs_tie_uses_silver_medal():
    a = Recommendation(card_id="a", card_name="Card A", miles=200, effective_mpd=4.0)
    b = Recommendation(card_id="b", card_name="Card B", miles=200, effective_mpd=4.0)
    out = format_recommendations([a, b], merchant="x", amount_sgd=50, is_fcy=False)
    assert "🥇" in out  # first card still gets gold positionally
    assert "🥈" in out  # second tied card gets silver
    assert "(tied)" in out  # tied note on second card


def test_format_card_list_empty(catalog):
    assert "haven't added" in format_card_list([], catalog).lower()


def test_format_card_list_with_cards(catalog):
    out = format_card_list(["hsbc_revo", "uob_ppv"], catalog)
    assert "HSBC Revolution" in out
    assert "UOB Preferred Platinum Visa" in out


# ---------------------------------------------------------------------------
# Inline keyboard formatters
# ---------------------------------------------------------------------------

def test_format_menu_keyboard_has_two_buttons(catalog):
    text, kb = format_menu_keyboard()
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    labels = [b.text for b in all_buttons]
    assert any("My Cards" in l for l in labels)
    assert any("Browse" in l for l in labels)


def test_format_menu_keyboard_button_data(catalog):
    _, kb = format_menu_keyboard()
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    data = {b.callback_data for b in all_buttons}
    assert "ck:wallet" in data
    assert "ck:catalog" in data


def test_format_wallet_keyboard_empty(catalog):
    text, kb = format_wallet_keyboard([], catalog)
    assert "empty" in text.lower() or "no cards" in text.lower() or "haven't" in text.lower()


def test_format_wallet_keyboard_has_back_button(catalog):
    _, kb = format_wallet_keyboard(["hsbc_revo"], catalog)
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    data = [b.callback_data for b in all_buttons]
    assert "ck:menu" in data


def test_format_catalog_keyboard_add_buttons(catalog):
    _, kb = format_catalog_keyboard(catalog, owned_ids=[])
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    data = [b.callback_data for b in all_buttons]
    assert "ck:add:hsbc_revo" in data


def test_format_catalog_keyboard_owned_marked(catalog):
    _, kb = format_catalog_keyboard(catalog, owned_ids=["hsbc_revo"])
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    revo_btn = next(b for b in all_buttons if "hsbc_revo" in (b.callback_data or ""))
    assert revo_btn.callback_data == "ck:rm:hsbc_revo"
    assert "✓" in revo_btn.text


def test_format_catalog_keyboard_has_back_button(catalog):
    _, kb = format_catalog_keyboard(catalog, owned_ids=[])
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    data = [b.callback_data for b in all_buttons]
    assert "ck:menu" in data


# ---------------------------------------------------------------------------
# v2: /log command
# ---------------------------------------------------------------------------


async def test_log_command_happy_path(storage, catalog, merchants):
    await storage.add_card(1, "uob_ppv")
    text, kb = await handle_log_command(
        ["uob_ppv", "shopee", "45"], 1, storage, catalog, merchants,
        today=date(2026, 5, 4),
    )
    assert "Logged" in text
    assert "UOB Preferred Platinum Visa" in text
    # Check transaction was stored
    rows = await storage.recent_transactions(1)
    assert len(rows) == 1
    assert rows[0].card_id == "uob_ppv"
    assert rows[0].amount_sgd == 45.0
    assert rows[0].miles_earned == 180  # 45 * 4mpd online bonus
    assert rows[0].bonus_idx == 1  # online retail bonus is idx 1


async def test_log_command_unknown_card(storage, catalog, merchants):
    text, kb = await handle_log_command(
        ["bogus_card", "shopee", "45"], 1, storage, catalog, merchants
    )
    assert "Unknown card id" in text


async def test_log_command_card_not_in_wallet(storage, catalog, merchants):
    text, kb = await handle_log_command(
        ["hsbc_revo", "shopee", "45"], 1, storage, catalog, merchants
    )
    assert "isn't in your wallet" in text


async def test_log_command_invalid_amount(storage, catalog, merchants):
    await storage.add_card(1, "hsbc_revo")
    text, _ = await handle_log_command(
        ["hsbc_revo", "shopee", "abc"], 1, storage, catalog, merchants
    )
    assert "Invalid amount" in text


async def test_log_command_with_explicit_date(storage, catalog, merchants):
    await storage.add_card(1, "uob_ppv")
    await handle_log_command(
        ["uob_ppv", "shopee", "45", "2026-04-15"], 1, storage, catalog, merchants
    )
    rows = await storage.recent_transactions(1)
    assert rows[0].txn_date == date(2026, 4, 15)


async def test_log_command_fcy_flag(storage, catalog, merchants):
    await storage.add_card(1, "maybank_xl")
    text, _ = await handle_log_command(
        ["maybank_xl", "klook", "320", "fcy"], 1, storage, catalog, merchants,
        today=date(2026, 5, 4),
    )
    assert "Logged" in text
    rows = await storage.recent_transactions(1)
    assert rows[0].is_fcy is True


async def test_log_command_short_args(storage, catalog, merchants):
    text, _ = await handle_log_command(["uob_ppv"], 1, storage, catalog, merchants)
    assert "Usage:" in text


async def test_log_command_unknown_extra(storage, catalog, merchants):
    await storage.add_card(1, "hsbc_revo")
    text, _ = await handle_log_command(
        ["hsbc_revo", "shopee", "45", "garbage"], 1, storage, catalog, merchants
    )
    assert "Couldn't parse" in text


# ---------------------------------------------------------------------------
# v2: /pools command
# ---------------------------------------------------------------------------


async def test_pools_command_empty_wallet(storage, catalog):
    text, kb = await handle_pools_command(1, storage, catalog, today=date(2026, 5, 4))
    assert "wallet is empty" in text.lower()
    assert kb is None


async def test_pools_command_renders_each_owned_card(storage, catalog):
    await storage.add_card(1, "hsbc_revo")
    await storage.add_card(1, "uob_ppv")
    text, _ = await handle_pools_command(1, storage, catalog, today=date(2026, 5, 4))
    assert "HSBC Revolution" in text
    assert "UOB Preferred Platinum Visa" in text


async def test_pools_warns_when_statement_day_missing(storage, catalog):
    await storage.add_card(1, "uob_vs")
    text, kb = await handle_pools_command(1, storage, catalog, today=date(2026, 5, 4))
    assert "statement day not set" in text
    # Button to set it should appear
    assert kb is not None
    all_btns = [b for row in kb.inline_keyboard for b in row]
    assert any("uob_vs" in (b.callback_data or "") for b in all_btns)


async def test_pools_groups_uob_unis_pool_footer(storage, catalog):
    await storage.add_card(1, "uob_ppv")
    await storage.add_card(1, "uob_prvi")
    text, _ = await handle_pools_command(1, storage, catalog, today=date(2026, 5, 4))
    assert "UOB UNI$" in text
    assert "redemption pool" in text.lower()


async def test_pools_reflects_logged_txn(storage, catalog, merchants):
    await storage.add_card(1, "uob_ppv")
    await handle_log_command(
        ["uob_ppv", "shopee", "200"], 1, storage, catalog, merchants,
        today=date(2026, 5, 4),
    )
    text, _ = await handle_pools_command(1, storage, catalog, today=date(2026, 5, 4))
    # online retail used: S$200 / S$600
    assert "200" in text
    assert "600" in text


# ---------------------------------------------------------------------------
# v2: /recent command
# ---------------------------------------------------------------------------


async def test_recent_command_empty(storage, catalog):
    text, kb = await handle_recent_command(1, storage, catalog)
    assert "No transactions" in text
    assert kb is None


async def test_recent_command_with_delete_button(storage, catalog, merchants):
    await storage.add_card(1, "hsbc_revo")
    await handle_log_command(
        ["hsbc_revo", "shopee", "50"], 1, storage, catalog, merchants
    )
    text, kb = await handle_recent_command(1, storage, catalog)
    assert "shopee" in text
    assert kb is not None
    all_btns = [b for row in kb.inline_keyboard for b in row]
    assert any((b.callback_data or "") == "recent:edit" for b in all_btns)


# ---------------------------------------------------------------------------
# v2: /lady_choice
# ---------------------------------------------------------------------------


async def test_lady_choice_command_no_args_renders_keyboard(storage):
    text, kb = await handle_lady_choice_command([], 1, storage, today=date(2026, 5, 4))
    assert "Lady" in text
    assert kb is not None
    btn_data = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert "lc:dining" in btn_data
    assert "lc:fashion" in btn_data
    assert "lc:travel" in btn_data


async def test_lady_choice_command_sets_category(storage):
    text, kb = await handle_lady_choice_command(
        ["dining_local"], 1, storage, today=date(2026, 5, 4)
    )
    assert "set" in text.lower()
    cat = await storage.get_lady_choice(1, today=date(2026, 5, 4))
    assert cat == "dining_local"


async def test_recommendation_uses_lady_choice_when_owned(storage, catalog, merchants):
    await storage.add_card(1, "uob_lady")
    await storage.set_lady_choice(1, "dining_local", effective_from=date(2026, 1, 1))
    payload = await compute_recommendation_payload(
        "kopitiam 50", 1, storage, catalog, merchants, today=date(2026, 5, 4)
    )
    # kopitiam → [dining_local, contactless]; lady_choice = dining_local → injects lady_chosen
    # Lady's bonus 4mpd fires: 50 * 4 = 200 mi
    assert payload.recs[0].miles == 200


# ---------------------------------------------------------------------------
# v2: statement-day prompt
# ---------------------------------------------------------------------------


def test_statement_day_prompt_keyboard(catalog):
    text, kb = format_statement_day_prompt(catalog["uob_vs"])
    assert "UOB Visa Signature" in text
    btn_data = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert "sday:uob_vs:1" in btn_data
    assert "sday:uob_vs:28" in btn_data
    assert "sday:uob_vs:skip" in btn_data


# ---------------------------------------------------------------------------
# v2: log inline buttons
# ---------------------------------------------------------------------------


def test_format_log_buttons_creates_token_per_card(catalog):
    rec1 = Recommendation(card_id="hsbc_revo", card_name="HSBC Revolution", miles=180, effective_mpd=4.0)
    rec2 = Recommendation(card_id="uob_ppv", card_name="UOB Preferred Platinum Visa", miles=200, effective_mpd=4.0)
    pending = {}
    kb = format_log_buttons(
        [rec1, rec2], pending, merchant="cold_storage", amount_sgd=45.0, is_fcy=False
    )
    assert kb is not None
    assert len(pending) == 2
    btn_data = [b.callback_data for row in kb.inline_keyboard for b in row]
    for d in btn_data:
        assert d.startswith("log:")
        token = d[len("log:"):]
        assert token in pending


def test_format_log_buttons_empty():
    assert format_log_buttons([], {}, merchant="x", amount_sgd=1, is_fcy=False) is None


# ---------------------------------------------------------------------------
# v2: ship-gate end-to-end
# ---------------------------------------------------------------------------


async def test_recommendation_uses_usage_state(storage, catalog, merchants):
    """Ship-gate: log fills UOB PPV mobile cap; subsequent recommendation
    should downrank UOB PPV in favor of HSBC Revo."""
    await storage.add_card(1, "hsbc_revo")
    await storage.add_card(1, "uob_ppv")
    today = date(2026, 5, 4)

    # Before logging: cold storage 45 → HSBC Revo wins via contactless 4mpd (180 mi).
    # UOB PPV doesn't match contactless category; this test focuses on PPV mobile_contactless.
    # Use a merchant that triggers the mobile_contactless bonus on PPV: grab_food.
    payload_before = await compute_recommendation_payload(
        "grab food 50", 1, storage, catalog, merchants, today=today
    )
    # PPV bonus 1 (online retail/grocery/food-delivery) + HSBC contactless
    # grab_food merchant maps to [dining_delivery, online_shopping, grab]
    # PPV bonus 1 fires (online_shopping, dining_delivery): 50*4 = 200
    # HSBC Revo: grab IS in revo categories, online_shopping too → 50*4 = 200
    # Both 200; alphabetic break HSBC < UOB → HSBC wins, but UOB is close behind.

    # Now fill UOB PPV's online retail cap (idx=1, S$600 cap_period=calendar_month)
    await handle_log_command(
        ["uob_ppv", "shopee", "600"], 1, storage, catalog, merchants, today=today
    )

    payload_after = await compute_recommendation_payload(
        "grab food 50", 1, storage, catalog, merchants, today=today
    )
    # After: UOB PPV bonus 1 cap reached, falls to base 0.4 → 20 mi
    # HSBC Revo unchanged at 200 mi → wins outright.
    hsbc = next(r for r in payload_after.recs if r.card_id == "hsbc_revo")
    ppv = next(r for r in payload_after.recs if r.card_id == "uob_ppv")
    assert hsbc.miles == 200
    assert ppv.miles == 20
    assert hsbc.miles > ppv.miles


async def test_recommendation_after_calendar_month_rollover(storage, catalog, merchants):
    """Period boundary: April txn fills UOB PPV mobile cap; on May 1 the
    cap is empty again."""
    await storage.add_card(1, "uob_ppv")
    await handle_log_command(
        ["uob_ppv", "grab_food", "600", "2026-04-28"],
        1, storage, catalog, merchants,
        today=date(2026, 4, 28),
    )
    # April: cap full
    payload_apr = await compute_recommendation_payload(
        "grab food 50", 1, storage, catalog, merchants, today=date(2026, 4, 30)
    )
    ppv_apr = next(r for r in payload_apr.recs if r.card_id == "uob_ppv")
    # cap reached → base only on 50: floor_sgd_5 = 50, base 0.4 → 20
    assert ppv_apr.miles == 20

    # May 1: new period, empty cap
    payload_may = await compute_recommendation_payload(
        "grab food 50", 1, storage, catalog, merchants, today=date(2026, 5, 1)
    )
    ppv_may = next(r for r in payload_may.recs if r.card_id == "uob_ppv")
    # bonus fires fully: 50 * 4 = 200
    assert ppv_may.miles == 200


# ---------------------------------------------------------------------------
# v3: posting_warning shown in format_recommendations
# ---------------------------------------------------------------------------


def test_format_recs_shows_posting_warning():
    rec = Recommendation(
        card_id="hsbc_revo", card_name="HSBC Revolution",
        miles=180, effective_mpd=4.0, reasons=["✓ online or contactless"],
        posting_warning="Posts Fri 1 May — counts toward May cap, not Apr",
    )
    out = format_recommendations(
        [rec], merchant="cold_storage", amount_sgd=45, is_fcy=False
    )
    assert "Posts Fri 1 May" in out
    assert "May cap" in out


def test_format_recs_no_posting_warning_when_none():
    rec = Recommendation(
        card_id="hsbc_revo", card_name="HSBC Revolution",
        miles=180, effective_mpd=4.0, reasons=["✓ online or contactless"],
        posting_warning=None,
    )
    out = format_recommendations(
        [rec], merchant="cold_storage", amount_sgd=45, is_fcy=False
    )
    assert "counts toward" not in out


# ---------------------------------------------------------------------------
# v3: /pools nudge for end-of-period
# ---------------------------------------------------------------------------


async def test_pools_nudge_appears_when_near_period_end(storage, catalog):
    """days_left <= delay+1 → ⏰ nudge line appears."""
    await storage.add_card(1, "hsbc_revo")
    await storage.set_posting_delay(1, "hsbc_revo", 1)
    # Apr 29: 1 day left in April, delay=1, threshold=1+1=2 → nudge fires
    text, _ = await handle_pools_command(1, storage, catalog, today=date(2026, 4, 29))
    assert "⏰" in text
    assert "HSBC" in text


async def test_pools_no_nudge_when_not_near_end(storage, catalog):
    """No nudge when far from period end."""
    await storage.add_card(1, "hsbc_revo")
    await storage.set_posting_delay(1, "hsbc_revo", 1)
    # May 4: 27 days left, well outside delay+1=2 threshold
    text, _ = await handle_pools_command(1, storage, catalog, today=date(2026, 5, 4))
    assert "⏰" not in text


# ---------------------------------------------------------------------------
# v3: compute_recommendation_payload uses posting delays
# ---------------------------------------------------------------------------


async def test_compute_payload_passes_posting_delays(storage, catalog, merchants):
    """Posting delays from storage are forwarded to recommend()."""
    await storage.add_card(1, "hsbc_revo")
    await storage.set_posting_delay(1, "hsbc_revo", 1)
    payload = await compute_recommendation_payload(
        "cold storage 45", 1, storage, catalog, merchants, today=date(2026, 4, 30)
    )
    assert payload.recs  # no error and recommendations produced


async def test_compute_payload_posting_warning_on_period_boundary(storage, catalog, merchants):
    """HSBC Revo T+1, Apr 30 → posting_date May 1 → posting_warning set."""
    await storage.add_card(1, "hsbc_revo")
    await storage.set_posting_delay(1, "hsbc_revo", 1)
    payload = await compute_recommendation_payload(
        "cold storage 45", 1, storage, catalog, merchants, today=date(2026, 4, 30)
    )
    revo = next(r for r in payload.recs if r.card_id == "hsbc_revo")
    assert revo.posting_warning is not None
    assert "May" in revo.posting_warning
    assert "Apr" in revo.posting_warning


async def test_compute_payload_no_warning_for_same_day_merchant(storage, catalog, merchants):
    """same_day_posting merchants post on txn_date → no period crossing."""
    await storage.add_card(1, "hsbc_revo")
    await storage.set_posting_delay(1, "hsbc_revo", 1)
    # grab is same_day_posting=True → posting_date = txn_date = Apr 30 → same period
    payload = await compute_recommendation_payload(
        "grab 45", 1, storage, catalog, merchants, today=date(2026, 4, 30)
    )
    revo = next(r for r in payload.recs if r.card_id == "hsbc_revo")
    assert revo.posting_warning is None


# ---------------------------------------------------------------------------
# v3: anniversary month prompt
# ---------------------------------------------------------------------------


def test_format_anniversary_prompt_has_month_buttons():
    from cardkaki.models import Card, Bonus, Rounding
    card = Card(
        id="kf_uob", name="KrisFlyer UOB",
        issuer="uob", network="visa",
        base_rate_mpd=1.2, rounding=Rounding(method="none"),
        bonus=[], anniversary_year=True,
    )
    text, kb = format_anniversary_prompt(card)
    assert "KrisFlyer UOB" in text
    btn_data = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert "ann:kf_uob:1" in btn_data
    assert "ann:kf_uob:12" in btn_data
    assert "ann:kf_uob:skip" in btn_data
