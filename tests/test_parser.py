import pytest

from cardkaki.parser import parse


def test_simple():
    p = parse("cold storage 45")
    assert p.merchant == "cold_storage"
    assert p.amount_sgd == 45.0
    assert p.is_fcy is False


def test_fcy_flag():
    p = parse("klook 320 fcy")
    assert p.merchant == "klook"
    assert p.amount_sgd == 320.0
    assert p.is_fcy is True


def test_case_insensitive():
    p = parse("Cold Storage 45")
    assert p.merchant == "cold_storage"


def test_decimal_amount():
    p = parse("cold storage 9.99")
    assert p.amount_sgd == 9.99


def test_explicit_sgd_flag():
    p = parse("mcdonalds 12.50 sgd")
    assert p.is_fcy is False
    assert p.amount_sgd == 12.50


def test_extra_whitespace():
    p = parse("   shopee   100   ")
    assert p.merchant == "shopee"
    assert p.amount_sgd == 100.0


def test_no_merchant_raises():
    with pytest.raises(ValueError):
        parse("45")


def test_no_amount_raises():
    with pytest.raises(ValueError):
        parse("klook abc")


def test_empty_raises():
    with pytest.raises(ValueError):
        parse("")


def test_unknown_flag_raises():
    with pytest.raises(ValueError):
        parse("klook 320 eur")


def test_single_word_merchant():
    p = parse("amazon 50")
    assert p.merchant == "amazon"


def test_three_word_merchant():
    p = parse("dbs womans world 75")
    assert p.merchant == "dbs_womans_world"
