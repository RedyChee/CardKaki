from pathlib import Path

import pytest

from cardkaki.bot import (
    format_card_list,
    format_catalog_keyboard,
    format_menu_keyboard,
    format_recommendations,
    format_wallet_keyboard,
    handle_cards_command,
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
    assert "Added" in reply
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
    assert "Removed" in reply
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
    assert "180 mi" in reply  # 45 * 4mpd, floor_sgd_1


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
    assert "180 mi" in out
    assert "4.0 mpd" in out


def test_format_recs_tie_uses_equal_marker():
    a = Recommendation(card_id="a", card_name="Card A", miles=200, effective_mpd=4.0)
    b = Recommendation(card_id="b", card_name="Card B", miles=200, effective_mpd=4.0)
    out = format_recommendations([a, b], merchant="x", amount_sgd=50, is_fcy=False)
    assert out.count("=") >= 2  # both top entries marked with =, no medal


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


def test_format_wallet_keyboard_remove_buttons(catalog):
    _, kb = format_wallet_keyboard(["hsbc_revo", "uob_ppv"], catalog)
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    data = [b.callback_data for b in all_buttons]
    assert "ck:rm:hsbc_revo" in data
    assert "ck:rm:uob_ppv" in data


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
    assert revo_btn.callback_data == "ck:own:hsbc_revo"
    assert "✓" in revo_btn.text


def test_format_catalog_keyboard_has_back_button(catalog):
    _, kb = format_catalog_keyboard(catalog, owned_ids=[])
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    data = [b.callback_data for b in all_buttons]
    assert "ck:menu" in data
