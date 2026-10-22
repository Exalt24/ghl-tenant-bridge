"""What Meta's Conversions API needs from a lead: attribution, hashed customer fields, a click id, a clean URL.

Where the attribution comes from is decided in `ghl.normalise_lead`: the nested `attributionSource` object of the Get
Contact shape first, then flat workflow body keys, and a value that is missing stays missing. This module turns that
into the request's `user_data`, `custom_data` and `event_source_url`.

Every rule cites its page in contracts/meta/SOURCE.md. The three that were not fully settled by a page are marked
ASSUMED here and in the README: phone numbers with no marker for a country code, the way a state outside the US is
normalised beyond the page's own sentence, and what a UTM value may contain (Meta documents no UTM fields at all).

Never hashed, by Meta's own page: client_ip_address, client_user_agent, fbc and fbp. NEVER_HASH names them so a test
can assert each one reaches the wire exactly as received.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, parse_qsl, urlsplit, urlunsplit

NEVER_HASH = ("client_ip_address", "client_user_agent", "fbc", "fbp")
HASHED_FIELDS = ("em", "ph", "fn", "ln", "ct", "st", "zp", "country")

CLICK_ID_PARAMS = frozenset({"fbclid", "gclid", "msclkid", "dclid", "gbraid", "wbraid", "ttclid"})
CLICK_ID_RE = re.compile(r"[A-Za-z0-9_\-.]{1,512}")
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
UTM_MAX_CHARS = 200
UTM_PUNCTUATION = " _.-+%:/"
URL_MAX_CHARS = 2000
MAX_USER_AGENT_CHARS = 1000
FBC_SUBDOMAIN_INDEX = 1  # the page: "If you're generating this field on a server ... use the value 1"

US_NAMES = {"us", "usa", "united states", "united states of america", "america"}
COUNTRY_ALIASES = {
    "usa": "us", "united states": "us", "united states of america": "us", "america": "us",
    "uk": "gb", "united kingdom": "gb", "great britain": "gb", "england": "gb",
    "philippines": "ph", "canada": "ca", "australia": "au", "new zealand": "nz", "ireland": "ie",
    "singapore": "sg", "india": "in", "germany": "de", "france": "fr", "mexico": "mx",
}
US_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca", "colorado": "co",
    "connecticut": "ct", "delaware": "de", "district of columbia": "dc", "florida": "fl", "georgia": "ga",
    "hawaii": "hi", "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md", "massachusetts": "ma",
    "michigan": "mi", "minnesota": "mn", "mississippi": "ms", "missouri": "mo", "montana": "mt",
    "nebraska": "ne", "nevada": "nv", "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm",
    "new york": "ny", "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc", "south dakota": "sd",
    "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
}
HEX64 = re.compile(r"[0-9a-f]{64}")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def looks_hashed(value: Any) -> bool:
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


# ---- normalisation, one function per field (customer-information-parameters, retrieved 2026-10-22) -----------------------


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s or None


def norm_email(value: Any) -> str | None:
    """Trim and lowercase. A value with no @ is not an email, so it is left out rather than hashed."""
    s = _clean(value)
    if s is None or "@" not in s:
        return None
    return s.lower()


def norm_phone(value: Any, default_country_code: str | None = None) -> str | None:
    """Digits only, no leading zeros, and a country code or nothing at all.

    The page: "Phone numbers must include a country code to be used for matching". A number says it has one with a
    leading `+` or `00`. A number that starts with a single `0` is national, and only then is the configured default
    country code put in front of it. Anything else is accepted only when it is long enough to already carry a code
    (11 to 15 digits, ASSUMED: the page gives no rule for a bare number). Otherwise it is left out, not sent wrong."""
    raw = _clean(value)
    if raw is None:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if raw.startswith("+"):
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        if not default_country_code:
            return None
        digits = default_country_code + digits[1:]
    elif len(digits) < 11:
        return None
    digits = digits.lstrip("0")
    if not 8 <= len(digits) <= 15:
        return None
    return digits


def _drop_punctuation(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch)[0] not in ("P", "S"))


def norm_name(value: Any) -> str | None:
    """fn and ln: lowercase, no punctuation, UTF-8 kept as it is ("Valéry" stays "valéry")."""
    s = _clean(value)
    if s is None:
        return None
    out = " ".join(_drop_punctuation(s.lower()).split())
    return out or None


def norm_city(value: Any) -> str | None:
    """Lowercase, no punctuation, no special characters, no spaces ("New York" is "newyork")."""
    s = norm_name(value)
    return s.replace(" ", "") if s else None


def norm_country(value: Any) -> str | None:
    """Lowercase ISO 3166-1 alpha-2. A two letter value passes, a few common names map, anything else is left out."""
    s = _clean(value)
    if s is None:
        return None
    low = s.lower()
    if re.fullmatch(r"[a-z]{2}", low):
        return low
    return COUNTRY_ALIASES.get(low)


def _is_us(country: Any) -> bool:
    """No country at all counts as the US (a lead form with a US state and no country field is the usual case)."""
    c = _clean(country)
    return c is None or c.lower() in US_NAMES


def norm_state(value: Any, country: Any = None) -> str | None:
    """US: the two character ANSI code in lowercase (a full state name maps to it). Outside the US: lowercase with no
    punctuation and no spaces, never cut to two letters (the page: "no punctuation, no special characters, no spaces")."""
    s = _clean(value)
    if s is None:
        return None
    low = " ".join(s.lower().split())
    if _is_us(country):
        if re.fullmatch(r"[a-z]{2}", low):
            return low
        mapped = US_STATES.get(low.replace(".", ""))
        if mapped:
            return mapped
    out = _drop_punctuation(low).replace(" ", "")
    return out or None


def norm_zip(value: Any, country: Any = None) -> str | None:
    """Lowercase with no spaces and no dash; a US zip is cut to its first 5 digits."""
    s = _clean(value)
    if s is None:
        return None
    if _is_us(country) and re.fullmatch(r"\d{5}(?:[- ]?\d{4})?", s):
        return s[:5]
    out = s.lower().replace(" ", "").replace("-", "")
    return out or None


def _split_name(lead: dict[str, Any]) -> tuple[str | None, str | None]:
    first, last = lead.get("first_name"), lead.get("last_name")
    if first or last:
        return first, last
    parts = (lead.get("name") or "").split()
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return (parts[0] if parts else None), None


def hashed_user_data(lead: dict[str, Any], default_country_code: str | None = None) -> dict[str, str]:
    """em, ph, fn, ln, ct, st, zp and country, each normalised then SHA-256 as lowercase hex. A field the lead lacks, or
    one that cannot be made valid, is simply absent: nothing is guessed."""
    first, last = _split_name(lead)
    country = lead.get("country")
    fields = {
        "em": norm_email(lead.get("email")),
        "ph": norm_phone(lead.get("phone"), default_country_code),
        "fn": norm_name(first),
        "ln": norm_name(last),
        "ct": norm_city(lead.get("city")),
        "st": norm_state(lead.get("state"), country),
        "zp": norm_zip(lead.get("postal_code"), country),
        "country": norm_country(country),
    }
    return {k: sha256_hex(v) for k, v in fields.items() if v}


# ---- time and the click id -----------------------------------------------------------------------------------------------


def parse_time(value: Any) -> float | None:
    """Seconds since the epoch from an ISO 8601 stamp ("2021-11-26T12:41:02.193Z" or "...+05:30"), or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def build_fbc(fbclid: str, creation_ms: int) -> str:
    """fb.1.<creation time in MILLISECONDS>.<fbclid>. The fbclid is case sensitive: "do not apply any modifications
    before using, such as lower or upper case"."""
    return f"fb.{FBC_SUBDOMAIN_INDEX}.{creation_ms}.{fbclid}"


def creation_ms(lead: dict[str, Any], received_epoch: float | None, fallback_epoch: float) -> int:
    """The page: use the time the click id was first observed or received. The contact's dateAdded is the closest thing
    a lead carries, then the time the relay stored the event."""
    when = parse_time(lead.get("date_added"))
    if when is None:
        when = received_epoch if received_epoch is not None else fallback_epoch
    return round(when * 1000)


def click_id(attr: dict[str, Any]) -> str | None:
    value = attr.get("fbclid")
    return value if isinstance(value, str) and CLICK_ID_RE.fullmatch(value) else None


def fbc_for(lead: dict[str, Any], received_epoch: float | None, fallback_epoch: float) -> str | None:
    attr = lead.get("attribution") or {}
    given = _clean(attr.get("fbc"))
    if given:
        return given
    fbclid = click_id(attr)
    if fbclid is None:
        return None
    return build_fbc(fbclid, creation_ms(lead, received_epoch, fallback_epoch))


def plain_user_data(lead: dict[str, Any], received_epoch: float | None, fallback_epoch: float) -> dict[str, str]:
    """The four fields Meta says must NOT be hashed, exactly as received."""
    attr = lead.get("attribution") or {}
    out: dict[str, str] = {}
    ip = _clean(attr.get("ip"))
    if ip:
        try:
            out["client_ip_address"] = str(ipaddress.ip_address(ip))
        except ValueError:
            pass
    ua = _clean(attr.get("user_agent"))
    if ua:
        out["client_user_agent"] = ua[:MAX_USER_AGENT_CHARS]
    fbc = fbc_for(lead, received_epoch, fallback_epoch)
    if fbc:
        out["fbc"] = fbc
    fbp = _clean(attr.get("fbp"))
    if fbp:
        out["fbp"] = fbp
    return out


def has_click_id(lead: dict[str, Any], allowed_sources: list[str] | None = None) -> bool:
    """A lead that came from an ad leaves one of these. A lead with none (an import, a manual entry) is not a conversion
    Meta should hear about, unless its source is one the tenant allowlisted."""
    attr = lead.get("attribution") or {}
    if any(attr.get(k) for k in ("fbc", "fbp", "fbclid")):
        return True
    source = _clean(lead.get("source"))
    return bool(source and allowed_sources and source.lower() in {a.lower() for a in allowed_sources})


# ---- no personal data in a URL or a UTM value ----------------------------------------------------------------------------


def sanitise_utm(value: Any) -> str | None:
    """Letters, digits, space and `_ . - + % : /` only, at most 200 characters. A value holding anything else (an @, an =
    or a & is how an email or a query string gets in) is dropped whole, not cleaned. ASSUMED: Meta documents no UTM
    fields, so this is the relay's own rule."""
    s = _clean(value)
    if s is None:
        return None
    if not all(ch.isalnum() or ch in UTM_PUNCTUATION for ch in s):
        return None
    return s[:UTM_MAX_CHARS]


def sanitise_url(url: Any) -> str | None:
    """Scheme, host and path stay. The query keeps only `utm_*` (each value cleaned like a UTM) and click id parameters.
    Userinfo and the fragment go. A page that carried `?email=..&phone=..` reaches Meta without them."""
    s = _clean(url)
    if s is None or len(s) > URL_MAX_CHARS:
        return None
    try:
        parts = urlsplit(s)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    kept: list[str] = []
    for key, val in parse_qsl(parts.query, keep_blank_values=False):
        low = key.lower()
        if low.startswith("utm_"):
            clean = sanitise_utm(val)
        elif low in CLICK_ID_PARAMS:
            clean = val if CLICK_ID_RE.fullmatch(val) else None
        else:
            clean = None
        if clean:
            kept.append(f"{quote(key, safe='_')}={quote(clean, safe='')}")
    netloc = parts.hostname + (f":{port}" if port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, "&".join(kept), ""))


def custom_data_utms(lead: dict[str, Any]) -> dict[str, str]:
    """UTMs go in custom_data by convention: Meta's Conversions API pages define no UTM fields for it."""
    attr = lead.get("attribution") or {}
    out: dict[str, str] = {}
    for key in UTM_KEYS:
        clean = sanitise_utm(attr.get(key))
        if clean:
            out[key] = clean
    return out
