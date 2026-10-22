"""Hashing vectors, the click id, and the no-personal-data-in-a-URL rules.

The hash values that appear as literals are the ones Meta prints on its own customer-information page (retrieved
2026-10-22, contracts/meta/SOURCE.md). For the fields whose page lists only a normalised example (city, state, zip), the
test hashes that example itself, so the vector is the page's text and the digest is computed, never typed from memory.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

import pytest

from relay import attribution as at
from relay import ghl

from .helpers import payload

HEX64 = re.compile(r"[0-9a-f]{64}")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


PHONE_16505551212 = "e323ec626319ca94ee8bff2e4c87cf613be6ea19919ed1364124e16807ab3176"


def lead_with(**over):
    return ghl.normalise_lead(payload(**over))


# ---- the page's own vectors -----------------------------------------------------------------------------------------------


def test_the_pages_own_hash_vectors_match_for_email_names_and_country():
    out = at.hashed_user_data({"email": "John_Smith@gmail.com", "first_name": "Valéry", "last_name": "정", "country": "United States"})
    assert out["em"] == "62a14e44f765419d10fea99367361a727c12365e2520f32218d505ed9aa0f62f"
    assert out["fn"] == "08e1996b5dd49e62a4b4c010d44e4345592a863bb9f8e3976219bac29417149c"
    assert out["ln"] == "8fa8cd9c440be61d0151429310034083132b35975c4bea67fdd74158eb51db14"
    assert out["country"] == "79adb2a2fce5c6ba215fe5f27f532d4e7edbac4b6a5e09e1ef3a08084a904621"
    assert at.hashed_user_data({"first_name": "Mary"})["fn"] == "6915771be1c5aa0c886870b6951b03d7eafc121fea0e80a5ea83beb7c449f4ec"


def test_the_pages_phone_example_hashes_to_the_pages_digest_from_any_formatting():
    for raw in ("+1 (650) 555-1212", "+16505551212", "001 650 555 1212", "1-650-555-1212 "):
        assert at.hashed_user_data({"phone": raw})["ph"] == PHONE_16505551212, raw


# ---- email -----------------------------------------------------------------------------------------------------------------


def test_email_is_trimmed_and_lowercased_before_hashing_and_an_address_with_no_at_sign_is_left_out():
    assert at.hashed_user_data({"email": "  Pat@Example.COM \n"})["em"] == sha("pat@example.com")
    assert at.hashed_user_data({"email": "pat@example.com"})["em"] == at.hashed_user_data({"email": "PAT@EXAMPLE.COM"})["em"]
    assert "em" not in at.hashed_user_data({"email": "not an email"})
    assert "em" not in at.hashed_user_data({"email": "   "})


# ---- phone -----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,cc,expected",
    [
        ("+63 917 123 4567", None, "639171234567"),  # a plus says it has a country code
        ("0063 917 123 4567", None, "639171234567"),  # 00 is the international prefix
        ("0917 123 4567", "63", "639171234567"),  # a single leading 0 is national: the default country code goes in front
        ("0917 123 4567", None, None),  # national and no default: left out, never sent wrong
        ("(650) 555-1212", None, None),  # 10 digits, no marker: no country code, left out
        ("16505551212", None, "16505551212"),  # 11 digits with no marker already carry the 1 (ASSUMED rule)
        ("+65 8123 4567", None, "6581234567"),  # a plus is trusted even when the number is short
        ("+0016505551212", None, "16505551212"),  # leading zeros are stripped
        ("12345", None, None),
        ("+1234567890123456789", None, None),  # more than 15 digits is not E.164
        ("abc", None, None),
        ("", None, None),
    ],
)
def test_phone_normalisation(raw, cc, expected):
    got = at.norm_phone(raw, cc)
    assert got == expected
    out = at.hashed_user_data({"phone": raw}, cc)
    assert (out.get("ph") == sha(expected)) if expected else ("ph" not in out)


def test_the_default_country_code_is_not_put_in_front_of_a_number_that_does_not_start_with_a_single_zero():
    assert at.norm_phone("00 917 123 4567", "63") == "9171234567"  # 00 means international, not national
    assert at.norm_phone("+63 917 123 4567", "1") == "639171234567"
    assert at.norm_phone("16505551212", "63") == "16505551212"


# ---- names, city, state, zip, country -----------------------------------------------------------------------------------------


def test_names_are_lowercased_stripped_of_punctuation_and_keep_their_utf8():
    assert at.norm_name("  Mary-Ann O'Brien ") == "maryann obrien"
    assert at.norm_name("Valéry") == "valéry"
    assert at.norm_name("!!!") is None
    assert at.hashed_user_data({"first_name": "Mary-Ann", "last_name": "O'Brien"}) == {"fn": sha("maryann"), "ln": sha("obrien")}


def test_a_full_name_is_split_when_there_is_no_first_and_last_name():
    out = at.hashed_user_data({"name": "Pat Q Lead"})
    assert out == {"fn": sha("pat"), "ln": sha("lead")}
    assert at.hashed_user_data({"name": "Cher"}) == {"fn": sha("cher")}


def test_city_has_no_punctuation_and_no_spaces():
    assert at.norm_city("New York") == "newyork"
    assert at.norm_city("St. Louis") == "stlouis"
    assert at.hashed_user_data({"city": "Paris"})["ct"] == sha("paris")


def test_a_us_state_is_its_two_letter_code_and_a_state_elsewhere_is_never_cut_to_two_letters():
    assert at.norm_state("TX", "US") == "tx"
    assert at.norm_state("Texas") == "tx"  # no country: a lead form with a US state and no country field
    assert at.norm_state("new york", "United States") == "ny"
    assert at.norm_state("Nuevo León", "MX") == "nuevoleón"
    assert at.norm_state("Metro Manila", "PH") == "metromanila"
    assert at.norm_state("Cebu", "PH") != "ce"
    assert at.norm_state("ON", "CA") == "on"  # two letters already, and not a US state name to map
    assert at.hashed_user_data({"state": "Az"})["st"] == sha("az")


def test_zip_is_lowercase_without_spaces_or_dashes_and_a_us_zip_is_five_digits():
    assert at.norm_zip("94035-1234") == "94035"
    assert at.norm_zip("94035 1234", "US") == "94035"
    assert at.norm_zip("94035") == "94035"
    assert at.norm_zip("M1 1AE", "GB") == "m11ae"
    assert at.norm_zip("SW1A 1AA") == "sw1a1aa"  # no country, but not shaped like a US zip: only lowercased and unspaced
    assert at.norm_zip("75018", "FR") == "75018"
    assert at.hashed_user_data({"postal_code": "M1 1AE", "country": "GB"})["zp"] == sha("m11ae")


def test_country_is_the_lowercase_two_letter_code_and_an_unknown_name_is_left_out():
    assert at.norm_country("US") == "us" and at.norm_country("United States") == "us" and at.norm_country("Philippines") == "ph"
    assert at.norm_country("Atlantis") is None and at.norm_country("") is None
    assert "country" not in at.hashed_user_data({"country": "Atlantis"})


def test_every_hashed_field_is_a_lowercase_sha256_hex_and_a_missing_field_is_absent():
    lead = lead_with(email="pat@example.com", postalCode="78701", country="US")
    out = at.hashed_user_data(lead)
    assert set(out) == set(at.HASHED_FIELDS)
    assert all(HEX64.fullmatch(v) for v in out.values())
    assert at.hashed_user_data({}) == {}


# ---- the four never-hash fields ------------------------------------------------------------------------------------------------


def test_the_never_hashed_fields_reach_the_wire_exactly_as_received_and_are_never_in_the_hashed_set():
    lead = ghl.normalise_lead(payload(**{"ip": "168.212.226.204", "userAgent": "Mozilla/5.0 (X11; Linux) Gecko/20100101", "fbc": "fb.1.1554763741205.AbCdEf", "fbp": "fb.1.1596403881668.1116446470"}))
    plain = at.plain_user_data(lead, None, 1_800_000_000.0)
    assert plain == {"client_ip_address": "168.212.226.204", "client_user_agent": "Mozilla/5.0 (X11; Linux) Gecko/20100101",
                     "fbc": "fb.1.1554763741205.AbCdEf", "fbp": "fb.1.1596403881668.1116446470"}
    assert at.NEVER_HASH == ("client_ip_address", "client_user_agent", "fbc", "fbp")
    assert not set(at.NEVER_HASH) & set(at.hashed_user_data(lead))
    for value in plain.values():
        assert not at.looks_hashed(value)


def test_an_ipv6_address_passes_and_a_malformed_one_is_left_out():
    v6 = ghl.normalise_lead(payload(ip="2001:0db8:85a3:0000:0000:8a2e:0370:7334"))
    assert at.plain_user_data(v6, None, 1.0)["client_ip_address"] == "2001:db8:85a3::8a2e:370:7334"
    assert "client_ip_address" not in at.plain_user_data(ghl.normalise_lead(payload(ip="not an ip")), None, 1.0)


# ---- fbc ---------------------------------------------------------------------------------------------------------------------------


PAGE_FBCLID = "IwAR2F4-dbP0l7Mn1IawQQGCINEz7PYXQvwjNwB_qa2ofrHyiLjcbCRxTDMgk"
PAGE_MS = 1554763741205


def iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def test_fbc_is_built_in_milliseconds_and_matches_the_pages_example():
    lead = ghl.normalise_lead(payload(fbclid=PAGE_FBCLID, dateAdded=iso_ms(PAGE_MS)))
    assert at.plain_user_data(lead, None, 0.0)["fbc"] == f"fb.1.{PAGE_MS}.{PAGE_FBCLID}"
    assert at.build_fbc("AbC", 1554763741205) == "fb.1.1554763741205.AbC"
    ms = int(at.plain_user_data(lead, None, 0.0)["fbc"].split(".")[2])
    assert ms > 10**12  # milliseconds, never seconds (13 digits, not 10)


def test_the_fbclid_keeps_its_case_exactly():
    lead = ghl.normalise_lead(payload(fbclid="AbCdEfGhIjKlMnOpQrStUvWxYz1234567890", dateAdded=iso_ms(PAGE_MS)))
    fbc = at.plain_user_data(lead, None, 0.0)["fbc"]
    assert fbc.endswith(".AbCdEfGhIjKlMnOpQrStUvWxYz1234567890") and fbc != fbc.lower()


def test_the_creation_time_is_the_contact_date_then_the_time_the_event_was_received_then_now():
    base = {"fbclid": "Abc123"}
    with_date = ghl.normalise_lead(payload(dateAdded=iso_ms(PAGE_MS), **base))
    assert at.plain_user_data(with_date, 1_700_000_000.0, 5.0)["fbc"] == f"fb.1.{PAGE_MS}.Abc123"
    no_date = ghl.normalise_lead(payload(dateAdded=None, **base))
    assert at.plain_user_data(no_date, 1_700_000_000.5, 5.0)["fbc"] == "fb.1.1700000000500.Abc123"
    assert at.plain_user_data(no_date, None, 5.0)["fbc"] == "fb.1.5000.Abc123"
    bad_date = ghl.normalise_lead(payload(dateAdded="yesterday", **base))
    assert at.plain_user_data(bad_date, 1_700_000_000.5, 5.0)["fbc"] == "fb.1.1700000000500.Abc123"


def test_a_given_fbc_wins_over_one_built_from_the_fbclid_and_no_click_id_means_no_fbc():
    lead = ghl.normalise_lead(payload(fbclid="Abc123", fbc="fb.1.111.FromCookie", dateAdded=iso_ms(PAGE_MS)))
    assert at.plain_user_data(lead, None, 0.0)["fbc"] == "fb.1.111.FromCookie"
    assert "fbc" not in at.plain_user_data(ghl.normalise_lead(payload()), None, 0.0)
    odd = ghl.normalise_lead(payload(fbclid="has space/and slash"))
    assert "fbc" not in at.plain_user_data(odd, None, 0.0)


def test_attribution_is_read_from_the_nested_get_contact_shape_and_from_flat_workflow_keys():
    nested = payload(attributionSource={"url": "https://x.example/lp?utm_source=fb", "utmSource": "facebook", "campaign": "spring", "fbclid": "Nested1", "userAgent": "UA-nested", "ip": "10.0.0.1", "fbEventId": "evt-nested"})
    flat = payload(utm_source="facebook", utm_campaign="spring", fbclid="Nested1", user_agent="UA-nested", ip="10.0.0.1", event_id="evt-nested", page_url="https://x.example/lp?utm_source=fb")
    for p in (nested, flat):
        lead = ghl.normalise_lead(p)
        assert lead["attribution"]["utm_source"] == "facebook" and lead["attribution"]["utm_campaign"] == "spring"
        assert at.plain_user_data(lead, None, 0.0)["client_user_agent"] == "UA-nested"
        assert at.click_id(lead["attribution"]) == "Nested1" and at.custom_data_utms(lead)["utm_source"] == "facebook"
        assert lead["attribution"]["fb_event_id"] == "evt-nested"


def test_a_lead_with_no_attribution_has_no_click_id_and_an_allowlisted_source_is_the_only_other_way_in():
    plain = ghl.normalise_lead(payload(source="Roof form"))
    assert not at.has_click_id(plain) and not at.has_click_id(plain, ["facebook"])
    assert at.has_click_id(plain, ["roof form"])  # case insensitive
    for key in ("fbc", "fbp", "fbclid"):
        assert at.has_click_id(ghl.normalise_lead(payload(**{key: "x1"})))


# ---- UTMs and URLs ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("spring_sale 2026", "spring_sale 2026"),
        ("a.b-c+d%20e:f/g", "a.b-c+d%20e:f/g"),
        ("promo=1", None),  # = is how a query string gets in
        ("a&b", None),
        ("john@example.com", None),  # an @ is how an email gets in
        ("x?y", None),
        ("<script>", None),
        ("", None),
        ("   ", None),
        ("é accents ok", "é accents ok"),
    ],
)
def test_utm_values_keep_only_letters_digits_space_and_the_listed_punctuation(value, expected):
    assert at.sanitise_utm(value) == expected


def test_a_utm_value_is_limited_to_200_characters_and_a_non_string_is_dropped():
    assert at.sanitise_utm("a" * 300) == "a" * 200
    assert at.sanitise_utm(5) is None and at.sanitise_utm(None) is None


def test_event_source_url_drops_personal_data_and_keeps_only_utm_and_click_id_parameters():
    url = "https://user:pw@Shop.Example.com:8443/thanks?email=pat%40example.com&phone=%2B14155550100&utm_source=facebook&fbclid=Ab12_x&name=Pat&utm_term=a%40b#frag"
    clean = at.sanitise_url(url)
    assert clean == "https://shop.example.com:8443/thanks?utm_source=facebook&fbclid=Ab12_x"
    for leak in ("email", "phone", "pat", "user", "pw", "frag", "a%40b", "name"):
        assert leak not in clean.lower().replace("shop.example.com", ""), leak
    assert at.sanitise_url("https://x.example/lp") == "https://x.example/lp"
    assert at.sanitise_url("https://x.example/lp?a=1&b=2") == "https://x.example/lp"


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "ftp://x.example/a", "not a url", "", None, 5, "https://", "https://x.example:99999/a", "http://" + "a" * 2100])
def test_a_url_that_is_not_http_or_is_unusable_is_dropped(bad):
    assert at.sanitise_url(bad) is None


def test_custom_data_utms_are_cleaned_and_only_the_five_known_keys_appear():
    lead = ghl.normalise_lead(payload(utm_source="fb", utm_medium="cpc", utm_campaign="a=b", utm_content="c", utm_term="d", utm_id="9"))
    assert at.custom_data_utms(lead) == {"utm_source": "fb", "utm_medium": "cpc", "utm_content": "c", "utm_term": "d"}


def test_parse_time_reads_utc_and_offset_stamps():
    assert at.parse_time("2021-11-26T12:41:02.193Z") == pytest.approx(1637930462.193)
    assert at.parse_time("2023-09-25T16:00:00+05:30") == at.parse_time("2023-09-25T10:30:00Z")
    assert at.parse_time("nope") is None and at.parse_time(None) is None and at.parse_time("") is None
