"""Contact keys: one person, one key, whatever shape the phone or email arrived in.

A history import and the send time check must agree on who a contact is, so both go through `contact_key`. A phone becomes
E.164 (`+15550101234`) and an email is lower cased, so `(555) 010-1234`, `555.010.1234`, `1 555 010 1234` and `+15550101234`
are one contact. The phone rules are a small table (`REGIONS`), not code per country: a calling code, the lengths a national
number may have and the trunk prefix people dial at home and drop abroad.

A refusal says what is wrong and never repeats the value: a contact is personal data and a reason ends up in a report and a log.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

DEFAULT_REGION = "US"
MAX_EMAIL_CHARS = 254
E164_MIN_DIGITS = 8
E164_MAX_DIGITS = 15
PHONE_CHARS = re.compile(r"^\+?[0-9 ().\-/]+$")
# One @, no spaces or the characters that make an address ambiguous, a dot inside the domain, no dot at either end of it.
EMAIL = re.compile(r"^[^@\s,;<>()\[\]\\\"]+@[^@\s,;<>()\[\]\\\".]+(?:\.[^@\s,;<>()\[\]\\\".]+)+$")


@dataclass(frozen=True)
class Region:
    code: str                    # the calling code, digits only
    national_lengths: tuple[int, ...]  # digits a national number has once the trunk prefix is dropped
    trunk: str | None = None     # the prefix dialled at home and dropped abroad


REGIONS: dict[str, Region] = {
    "US": Region("1", (10,)),
    "CA": Region("1", (10,)),
    "PH": Region("63", (10,), "0"),
    "GB": Region("44", (10,), "0"),
    "AU": Region("61", (9,), "0"),
    "NZ": Region("64", (8, 9), "0"),
    "IN": Region("91", (10,), "0"),
    "SG": Region("65", (8,)),
    "IE": Region("353", (8, 9), "0"),
}
# Calling codes, longest first, so `+353` is never read as a shorter code. Shared codes (US and CA) keep the first region's lengths.
CODES: dict[str, Region] = {}
for _name, _r in REGIONS.items():
    CODES.setdefault(_r.code, _r)
CODE_ORDER = sorted(CODES, key=len, reverse=True)


class ContactError(ValueError):
    """A readable reason that never contains the contact."""


def default_region(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    region = (env.get("RELAY_DEFAULT_REGION") or DEFAULT_REGION).strip().upper()
    if region not in REGIONS:
        raise ContactError(f"RELAY_DEFAULT_REGION must be one of {', '.join(sorted(REGIONS))}")
    return region


def normalise_phone(raw: object, region: str = DEFAULT_REGION) -> str:
    """E.164 text for a phone, or a ContactError. `region` says how a number with no country is read."""
    if region not in REGIONS:
        raise ContactError(f"unknown region, use one of {', '.join(sorted(REGIONS))}")
    if not isinstance(raw, str) or not raw.strip():
        raise ContactError("phone is empty")
    text = raw.strip()
    if not PHONE_CHARS.match(text) or "+" in text[1:]:
        raise ContactError("phone has characters other than digits, spaces, dashes, dots, slashes, brackets and a leading plus")
    digits = "".join(ch for ch in text if ch.isdigit())
    if text.startswith("+"):
        return _international(digits)
    if digits.startswith("00"):
        return _international(digits[2:])
    home = REGIONS[region]
    national = digits
    if home.trunk and national.startswith(home.trunk) and (len(national) - len(home.trunk)) in home.national_lengths:
        national = national[len(home.trunk):]
    elif len(national) not in home.national_lengths:
        # The country code typed without a plus: 15550101234 in the US, 639171234567 in the Philippines.
        if national.startswith(home.code) and (len(national) - len(home.code)) in home.national_lengths:
            national = national[len(home.code):]
        else:
            raise ContactError(f"phone is not a valid {region} number: {len(digits)} digits")
    if national.startswith("0"):
        raise ContactError(f"phone is not a valid {region} number: a national number never starts with 0")
    return "+" + home.code + national


def _international(digits: str) -> str:
    if not digits or digits.startswith("0"):
        raise ContactError("phone has no valid country code")
    if not E164_MIN_DIGITS <= len(digits) <= E164_MAX_DIGITS:
        raise ContactError(f"phone has {len(digits)} digits, an international number has {E164_MIN_DIGITS} to {E164_MAX_DIGITS}")
    for code in CODE_ORDER:
        if digits.startswith(code):
            national = digits[len(code):]
            if len(national) not in CODES[code].national_lengths:
                raise ContactError(f"phone is not a valid +{code} number: {len(national)} digits after the country code")
            break
    return "+" + digits


def normalise_email(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ContactError("email is empty")
    text = raw.strip().lower()
    if len(text) > MAX_EMAIL_CHARS or not EMAIL.match(text):
        raise ContactError("email is not a valid address")
    return text


def contact_key(raw: object, region: str = DEFAULT_REGION) -> str:
    """`email:<address>` or `phone:<E.164>`. An at sign decides it is an email, so a phone is never mistaken for one."""
    if not isinstance(raw, str) or not raw.strip():
        raise ContactError("contact is empty")
    if "@" in raw:
        return "email:" + normalise_email(raw)
    return "phone:" + normalise_phone(raw, region)


def lead_keys(lead: dict, region: str = DEFAULT_REGION) -> list[str]:
    """The keys a canonical lead can be known by (its email and its phone). A value that is not a valid contact is left out:
    it cannot match anything on a list."""
    keys: list[str] = []
    for field_name in ("email", "phone"):
        value = lead.get(field_name)
        if value is None:
            continue
        try:
            key = contact_key(value, region)
        except ContactError:
            continue
        if key not in keys:
            keys.append(key)
    return keys
