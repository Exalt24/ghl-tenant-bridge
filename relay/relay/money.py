"""Money: decimal text and JSON numbers to integer minor units. No float arithmetic anywhere on an amount.

Three rules, each with a test and a mutant:
  - An amount is parsed with `Decimal` from TEXT. A JSON number is turned into text first (`Decimal(str(x))`), because
    `Decimal(0.29)` carries the binary error of the float and `Decimal("0.29")` does not.
  - The number of minor units per major unit comes from an ISO 4217 exponent table (2 by default, 0 for JPY and KRW and
    the other zero decimal codes, 3 for the dinar family). The table and the code list were written from the standard
    and not retrieved from a saved page, so contracts/meta/SOURCE.md tags them ASSUMED: a valid code that is refused is
    fixed by adding it here.
  - A code that is not in the list is refused, never guessed. A float, a bool, a NaN, an exponent form like `1e3`, a
    thousands separator and a bare `.5` are refused too: an export that uses them needs cleaning, and silently
    misreading money is worse than stopping.

`exact=True` (the payments CSV) refuses an amount with more decimals than the currency has, so a value is never rounded
without anyone knowing. `exact=False` (ad spend from an API) rounds half up to the minor unit, because the vendor's own
figures can carry more decimals than the currency settles in.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

__all__ = ["MoneyError", "check_currency", "exponent", "parse_decimal_text", "to_minor_from_number", "to_minor_from_text"]


class MoneyError(ValueError):
    """A readable reason. Holds an amount or a code, never a secret."""


ZERO_DECIMAL = frozenset("BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX UYI VND VUV XAF XOF XPF".split())
THREE_DECIMAL = frozenset("BHD IQD JOD KWD LYD OMR TND".split())
FOUR_DECIMAL = frozenset("CLF UYW".split())
KNOWN_CURRENCIES = frozenset(
    """AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF
    CHE CHF CHW CLF CLP CNY COP COU CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ
    GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD
    MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG
    QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS
    UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWG""".split()
)
MAX_MAJOR_UNITS = Decimal(10) ** 15  # a bound far above any real spend, so a corrupt value cannot become a huge integer
_TEXT = re.compile(r"-?[0-9]+(\.[0-9]+)?")


def check_currency(code: object) -> str:
    if not isinstance(code, str) or code not in KNOWN_CURRENCIES:
        raise MoneyError("unknown currency code")
    return code


def exponent(currency: str) -> int:
    check_currency(currency)
    if currency in ZERO_DECIMAL:
        return 0
    if currency in THREE_DECIMAL:
        return 3
    if currency in FOUR_DECIMAL:
        return 4
    return 2


def parse_decimal_text(text: object) -> Decimal:
    """A plain decimal string: optional minus, digits, optional dot and digits. Nothing else."""
    if isinstance(text, bool) or not isinstance(text, str):
        raise MoneyError("an amount must be text, never a number type")
    cleaned = text.strip()
    if not _TEXT.fullmatch(cleaned):
        raise MoneyError("not a plain decimal amount")
    try:
        return Decimal(cleaned)
    except InvalidOperation:  # unreachable after the pattern, kept so a Decimal change cannot leak a raw exception
        raise MoneyError("not a plain decimal amount") from None


def _minor(value: Decimal, currency: str, *, exact: bool, allow_negative: bool) -> int:
    exp = exponent(currency)
    if not value.is_finite():
        raise MoneyError("amount is not a finite number")
    if value < 0 and not allow_negative:
        raise MoneyError("amount is negative")
    if abs(value) >= MAX_MAJOR_UNITS:
        raise MoneyError("amount is out of range")
    with localcontext() as ctx:
        ctx.prec = 60
        scaled = value.scaleb(exp)
        whole = scaled.to_integral_value(rounding=ROUND_HALF_UP)
        if exact and whole != scaled:
            raise MoneyError("amount has more decimals than the currency allows")
        return int(whole)


def to_minor_from_text(text: object, currency: str, *, exact: bool = False, allow_negative: bool = False) -> int:
    """`"12.34"` and `"USD"` give 1234, `"1500"` and `"JPY"` give 1500."""
    return _minor(parse_decimal_text(text), currency, exact=exact, allow_negative=allow_negative)


def to_minor_from_number(x: object, currency: str, *, allow_negative: bool = False) -> int:
    """A JSON number (an int or a float as `json.loads` made it). `Decimal(str(x))`, never `Decimal(x)`: the text of a
    float is its shortest round trip form, which is what the vendor wrote."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise MoneyError("an amount must be a JSON number")
    try:
        value = Decimal(str(x))
    except InvalidOperation:
        raise MoneyError("amount is not a number") from None
    return _minor(value, currency, exact=False, allow_negative=allow_negative)
