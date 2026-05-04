from datetime import date

import pytest

from cardkaki.storage import Storage


@pytest.fixture
async def storage(tmp_path):
    s = Storage(tmp_path / "users.sqlite")
    await s.init()
    return s


# ---------------------------------------------------------------------------
# v1 wallet store
# ---------------------------------------------------------------------------


async def test_init_creates_db(storage, tmp_path):
    assert (tmp_path / "users.sqlite").exists()


async def test_add_card_returns_true_then_false(storage):
    assert await storage.add_card(1, "hsbc_revo") is True
    assert await storage.add_card(1, "hsbc_revo") is False


async def test_remove_card(storage):
    await storage.add_card(1, "hsbc_revo")
    assert await storage.remove_card(1, "hsbc_revo") is True
    assert await storage.remove_card(1, "hsbc_revo") is False


async def test_list_cards(storage):
    await storage.add_card(1, "hsbc_revo")
    await storage.add_card(1, "uob_ppv")
    cards = await storage.list_cards(1)
    assert set(cards) == {"hsbc_revo", "uob_ppv"}


async def test_cross_user_isolation(storage):
    await storage.add_card(1, "hsbc_revo")
    await storage.add_card(2, "uob_ppv")
    assert await storage.list_cards(1) == ["hsbc_revo"]
    assert await storage.list_cards(2) == ["uob_ppv"]


async def test_list_cards_empty_user(storage):
    assert await storage.list_cards(999) == []


# ---------------------------------------------------------------------------
# v2: transactions
# ---------------------------------------------------------------------------


async def test_log_transaction_returns_tx_id(storage):
    tx_id = await storage.log_transaction(
        telegram_user_id=1,
        card_id="uob_ppv",
        bonus_idx=0,
        bonus_label="mobile contactless",
        merchant="grab_food",
        amount_sgd=45.0,
        is_fcy=False,
        miles_earned=180,
        txn_date=date(2026, 5, 4),
    )
    assert isinstance(tx_id, str) and len(tx_id) >= 16


async def test_recent_transactions_returns_descending(storage):
    await storage.log_transaction(
        telegram_user_id=1, card_id="hsbc_revo", bonus_idx=0, bonus_label="x",
        merchant="a", amount_sgd=10, is_fcy=False, miles_earned=40,
        txn_date=date(2026, 5, 1),
    )
    await storage.log_transaction(
        telegram_user_id=1, card_id="hsbc_revo", bonus_idx=0, bonus_label="x",
        merchant="b", amount_sgd=20, is_fcy=False, miles_earned=80,
        txn_date=date(2026, 5, 3),
    )
    rows = await storage.recent_transactions(1, limit=10)
    assert [r.merchant for r in rows] == ["b", "a"]


async def test_list_transactions_since_filters_by_date(storage):
    await storage.log_transaction(
        telegram_user_id=1, card_id="hsbc_revo", bonus_idx=0, bonus_label="x",
        merchant="april", amount_sgd=10, is_fcy=False, miles_earned=40,
        txn_date=date(2026, 4, 28),
    )
    await storage.log_transaction(
        telegram_user_id=1, card_id="hsbc_revo", bonus_idx=0, bonus_label="x",
        merchant="may", amount_sgd=20, is_fcy=False, miles_earned=80,
        txn_date=date(2026, 5, 1),
    )
    rows = await storage.list_transactions_since(1, since=date(2026, 5, 1))
    assert [r.merchant for r in rows] == ["may"]


async def test_delete_transaction_other_user_cannot_delete(storage):
    tx_id = await storage.log_transaction(
        telegram_user_id=1, card_id="hsbc_revo", bonus_idx=None, bonus_label=None,
        merchant="a", amount_sgd=10, is_fcy=False, miles_earned=4,
        txn_date=date(2026, 5, 1),
    )
    assert await storage.delete_transaction(2, tx_id) is False
    rows = await storage.list_transactions_since(1, since=date(2026, 1, 1))
    assert len(rows) == 1
    assert await storage.delete_transaction(1, tx_id) is True
    rows = await storage.list_transactions_since(1, since=date(2026, 1, 1))
    assert rows == []


async def test_transactions_cascade_on_user_delete(storage, tmp_path):
    import aiosqlite

    await storage.log_transaction(
        telegram_user_id=1, card_id="hsbc_revo", bonus_idx=None, bonus_label=None,
        merchant="a", amount_sgd=10, is_fcy=False, miles_earned=4,
        txn_date=date(2026, 5, 1),
    )
    async with aiosqlite.connect(tmp_path / "users.sqlite") as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM users WHERE telegram_user_id = 1")
        await db.commit()
    rows = await storage.list_transactions_since(1, since=date(2026, 1, 1))
    assert rows == []


async def test_log_transaction_preserves_bonus_idx_none(storage):
    tx_id = await storage.log_transaction(
        telegram_user_id=1, card_id="dbs_altitude", bonus_idx=None, bonus_label=None,
        merchant="a", amount_sgd=50, is_fcy=False, miles_earned=65,
        txn_date=date(2026, 5, 1),
    )
    rows = await storage.recent_transactions(1)
    assert rows[0].tx_id == tx_id
    assert rows[0].bonus_idx is None
    assert rows[0].bonus_label is None


# ---------------------------------------------------------------------------
# v2: statement days
# ---------------------------------------------------------------------------


async def test_set_statement_day_round_trip(storage):
    await storage.set_statement_day(1, "uob_vs", 22)
    days = await storage.get_statement_days(1)
    assert days == {"uob_vs": 22}


async def test_set_statement_day_overwrites(storage):
    await storage.set_statement_day(1, "uob_vs", 22)
    await storage.set_statement_day(1, "uob_vs", 25)
    days = await storage.get_statement_days(1)
    assert days == {"uob_vs": 25}


async def test_set_statement_day_rejects_out_of_range(storage):
    with pytest.raises(ValueError):
        await storage.set_statement_day(1, "uob_vs", 0)
    with pytest.raises(ValueError):
        await storage.set_statement_day(1, "uob_vs", 29)


async def test_get_statement_days_returns_all_for_user(storage):
    await storage.set_statement_day(1, "uob_vs", 22)
    await storage.set_statement_day(1, "citi_rewards", 5)
    await storage.set_statement_day(2, "uob_vs", 10)
    days = await storage.get_statement_days(1)
    assert days == {"uob_vs": 22, "citi_rewards": 5}


# ---------------------------------------------------------------------------
# v2: lady's chosen category
# ---------------------------------------------------------------------------


async def test_lady_choice_round_trip(storage):
    await storage.set_lady_choice(1, "dining_local", date(2026, 4, 1))
    cat = await storage.get_lady_choice(1, today=date(2026, 5, 4))
    assert cat == "dining_local"


async def test_get_lady_choice_returns_most_recent_at_or_before_today(storage):
    await storage.set_lady_choice(1, "dining_local", date(2026, 1, 1))
    await storage.set_lady_choice(1, "online_shopping", date(2026, 4, 1))
    # Before second pick
    assert await storage.get_lady_choice(1, today=date(2026, 3, 30)) == "dining_local"
    # On second pick
    assert await storage.get_lady_choice(1, today=date(2026, 4, 1)) == "online_shopping"
    # After second pick
    assert await storage.get_lady_choice(1, today=date(2026, 5, 1)) == "online_shopping"


async def test_get_lady_choice_none_when_unset(storage):
    assert await storage.get_lady_choice(1, today=date(2026, 5, 1)) is None


# ---------------------------------------------------------------------------
# v3: posting delays
# ---------------------------------------------------------------------------


async def test_set_posting_delay_round_trip(storage):
    await storage.set_posting_delay(1, "hsbc_revo", 1)
    delays = await storage.get_posting_delays(1)
    assert delays == {"hsbc_revo": 1}


async def test_set_posting_delay_overwrites(storage):
    await storage.set_posting_delay(1, "hsbc_revo", 1)
    await storage.set_posting_delay(1, "hsbc_revo", 3)
    delays = await storage.get_posting_delays(1)
    assert delays == {"hsbc_revo": 3}


async def test_set_posting_delay_rejects_out_of_range(storage):
    with pytest.raises(ValueError):
        await storage.set_posting_delay(1, "hsbc_revo", -1)
    with pytest.raises(ValueError):
        await storage.set_posting_delay(1, "hsbc_revo", 8)


async def test_get_posting_delays_returns_all_for_user(storage):
    await storage.set_posting_delay(1, "hsbc_revo", 1)
    await storage.set_posting_delay(1, "uob_ppv", 2)
    await storage.set_posting_delay(2, "hsbc_revo", 3)
    delays = await storage.get_posting_delays(1)
    assert delays == {"hsbc_revo": 1, "uob_ppv": 2}


async def test_get_posting_delays_empty(storage):
    assert await storage.get_posting_delays(999) == {}


# ---------------------------------------------------------------------------
# v3: card anniversaries
# ---------------------------------------------------------------------------


async def test_set_anniversary_round_trip(storage):
    await storage.set_anniversary(1, "krisflyer_uob", 3)
    months = await storage.get_anniversaries(1)
    assert months == {"krisflyer_uob": 3}


async def test_set_anniversary_overwrites(storage):
    await storage.set_anniversary(1, "krisflyer_uob", 3)
    await storage.set_anniversary(1, "krisflyer_uob", 6)
    months = await storage.get_anniversaries(1)
    assert months == {"krisflyer_uob": 6}


async def test_set_anniversary_rejects_out_of_range(storage):
    with pytest.raises(ValueError):
        await storage.set_anniversary(1, "krisflyer_uob", 0)
    with pytest.raises(ValueError):
        await storage.set_anniversary(1, "krisflyer_uob", 13)


async def test_get_anniversaries_returns_all_for_user(storage):
    await storage.set_anniversary(1, "krisflyer_uob", 3)
    await storage.set_anniversary(1, "amex_kf", 7)
    await storage.set_anniversary(2, "krisflyer_uob", 1)
    months = await storage.get_anniversaries(1)
    assert months == {"krisflyer_uob": 3, "amex_kf": 7}


async def test_get_anniversaries_empty(storage):
    assert await storage.get_anniversaries(999) == {}
