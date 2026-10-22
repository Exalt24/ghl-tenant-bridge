"""Money parsing: Decimal from text, an ISO 4217 exponent table, and what is refused. The values below are chosen so that
a float based parse gives a different answer (1.15 * 100 is 114.99999999999999 in binary floating point)."""

from __future__ import annotations

import pytest

from relay import money
from relay.money import MoneyError, exponent, to_minor_from_number, to_minor_from_text


def test_text_amounts_become_exact_minor_units():
    assert to_minor_from_text("12.34", "USD") == 1234  # control: the plain case
    assert to_minor_from_text("1.15", "USD") == 115  # float gives 114
    assert to_minor_from_text("0.29", "USD") == 29  # float gives 28
    assert to_minor_from_text("4.35", "EUR") == 435
    assert to_minor_from_text("5339.5", "USD") == 533950
    assert to_minor_from_text("007.50", "USD") == 750
    assert to_minor_from_text(" 12.00 ", "USD") == 1200


def test_the_exponent_table_follows_the_currency():
    assert (exponent("USD"), exponent("EUR"), exponent("PHP"), exponent("AUD")) == (2, 2, 2, 2)
    assert (exponent("JPY"), exponent("KRW"), exponent("VND"), exponent("CLP")) == (0, 0, 0, 0)
    assert (exponent("KWD"), exponent("BHD"), exponent("JOD")) == (3, 3, 3)
    assert exponent("CLF") == 4
    assert to_minor_from_text("1500", "JPY") == 1500
    assert to_minor_from_text("12000", "KRW") == 12000
    assert to_minor_from_text("1.234", "KWD") == 1234
    assert to_minor_from_text("1.5", "JPY") == 2  # not exact, rounds half up to a whole yen


def test_rounding_is_half_up_for_a_vendor_figure_and_refused_for_an_exact_one():
    assert to_minor_from_text("1.005", "USD") == 101
    assert to_minor_from_text("1.004", "USD") == 100
    assert to_minor_from_text("2.5", "JPY") == 3
    with pytest.raises(MoneyError, match="more decimals"):
        to_minor_from_text("1.005", "USD", exact=True)
    assert to_minor_from_text("10.500", "USD", exact=True) == 1050  # trailing zeros are not extra precision
    assert to_minor_from_text("1500.0", "JPY", exact=True) == 1500
    for text, minor in (("0.29", 29), ("1.15", 115), ("4.35", 435), ("19.99", 1999), ("0.07", 7)):
        assert to_minor_from_text(text, "USD", exact=True) == minor  # a float read of these is a hair off and would be refused as "more decimals"
    with pytest.raises(MoneyError, match="more decimals"):
        to_minor_from_text("1500.5", "JPY", exact=True)


def test_a_json_number_goes_through_its_text_not_its_binary_value():
    assert to_minor_from_number(125.45, "USD") == 12545
    assert to_minor_from_number(0.29, "USD") == 29
    assert to_minor_from_number(1.005, "USD") == 101  # Decimal(1.005) is 1.00499999999999989 and would give 100
    assert to_minor_from_number(1234567.89, "USD") == 123456789
    assert to_minor_from_number(7, "USD") == 700
    assert to_minor_from_number(1500.0, "JPY") == 1500
    assert to_minor_from_number(1e-07, "USD") == 0  # an exponent form that json.loads can produce


@pytest.mark.parametrize("bad", [12.5, 12, None, True, b"12.5", ["12.5"]])
def test_text_parsing_refuses_anything_that_is_not_text(bad):
    assert to_minor_from_text("12.5", "USD") == 1250  # control
    with pytest.raises(MoneyError, match="text"):
        to_minor_from_text(bad, "USD")


@pytest.mark.parametrize("bad", ["", "abc", "1e3", "1E3", "NaN", "Infinity", "-Infinity", "1,234.50", "$12.50", "12.5.1", ".5", "5.", "(12.50)", "+12.50", "1 234", "0x10", "١٢"])
def test_text_parsing_refuses_every_form_that_is_not_a_plain_decimal(bad):
    assert to_minor_from_text("12.50", "USD") == 1250  # control
    with pytest.raises(MoneyError):
        to_minor_from_text(bad, "USD", allow_negative=True)


@pytest.mark.parametrize("bad", ["12.5", None, True, False, b"1", [1]])
def test_number_parsing_refuses_anything_that_is_not_a_number(bad):
    assert to_minor_from_number(12.5, "USD") == 1250  # control
    with pytest.raises(MoneyError, match="JSON number"):
        to_minor_from_number(bad, "USD")


def test_number_parsing_refuses_nan_and_infinity():
    assert to_minor_from_number(1.5, "USD") == 150  # control
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(MoneyError):
            to_minor_from_number(bad, "USD", allow_negative=True)


def test_negative_amounts_need_to_be_allowed():
    assert to_minor_from_text("-12.50", "USD", allow_negative=True) == -1250
    assert to_minor_from_number(-3.5, "USD", allow_negative=True) == -350
    with pytest.raises(MoneyError, match="negative"):
        to_minor_from_text("-12.50", "USD")
    with pytest.raises(MoneyError, match="negative"):
        to_minor_from_number(-3.5, "USD")
    assert to_minor_from_text("-0", "USD") == 0  # minus zero is not a negative amount


@pytest.mark.parametrize("bad", ["usd", "US", "USDX", "XXX", "ZZZ", "", None, 840, "US$"])
def test_an_unknown_currency_is_refused_not_guessed(bad):
    assert to_minor_from_text("1.00", "USD") == 100  # control
    with pytest.raises(MoneyError, match="currency"):
        to_minor_from_text("1.00", bad)
    with pytest.raises(MoneyError, match="currency"):
        money.check_currency(bad)


def test_an_amount_beyond_any_real_spend_is_refused():
    assert to_minor_from_text("999999999999999", "USD") == 99999999999999900  # just under the bound
    with pytest.raises(MoneyError, match="range"):
        to_minor_from_text("1000000000000000", "USD")
    with pytest.raises(MoneyError, match="range"):
        to_minor_from_number(1e16, "USD")


def test_the_known_codes_include_every_zero_decimal_code_and_no_lower_case():
    assert money.ZERO_DECIMAL <= money.KNOWN_CURRENCIES and money.THREE_DECIMAL <= money.KNOWN_CURRENCIES
    assert all(len(c) == 3 and c.isupper() for c in money.KNOWN_CURRENCIES)
    assert {"USD", "EUR", "GBP", "AUD", "PHP", "CAD", "JPY", "KRW", "NZD", "SGD"} <= money.KNOWN_CURRENCIES
