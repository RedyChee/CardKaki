import pytest

from cardkaki.storage import Storage


@pytest.fixture
async def storage(tmp_path):
    s = Storage(tmp_path / "users.sqlite")
    await s.init()
    return s


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
