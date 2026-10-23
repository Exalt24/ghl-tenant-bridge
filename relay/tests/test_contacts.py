"""Contact keys: formats crossed with country shapes, in both directions (what must be one contact, what must be refused)."""

from __future__ import annotations

import itertools
import random

import pytest

from relay import contacts
from relay.contacts import REGIONS, ContactError, contact_key, lead_keys, normalise_email, normalise_phone

# One national number per country shape: a region, its national digits, and the region used to read the "+" forms (never the
# region itself, so an international number cannot pass by being read as a local one).
SHAPES = {
    "US": "5550101234",
    "CA": "4165550123",
    "PH": "9171234567",
    "GB": "7911123456",
    "AU": "412345678",
    "NZ": "211234567",
    "IN": "9876543210",
    "SG": "81234567",
    "IE": "851234567",
}


def groupings(d: str) -> dict[str, str]:
    a, b = d[: len(d) // 3], d[len(d) // 3 : 2 * len(d) // 3]
    c = d[2 * len(d) // 3 :]
    return {
        "plain": d,
        "spaced": f"{a} {b} {c}",
        "dashed": f"{a}-{b}-{c}",
        "dotted": f"{a}.{b}.{c}",
        "bracketed": f"({a}) {b}-{c}",
        "padded": f"  {a} {b}{c}  ",
    }


def prefixes(region: str) -> dict[str, str]:
    r = REGIONS[region]
    out = {"none": "", "code": r.code + " ", "plus": "+" + r.code + " ", "double zero": "00" + r.code + " "}
    if r.trunk:
        out["trunk"] = r.trunk
    return out


def cases():
    for region, national in SHAPES.items():
        expected = "+" + REGIONS[region].code + national
        for (gname, grouped), (pname, prefix) in itertools.product(groupings(national).items(), prefixes(region).items()):
            if pname == "trunk" and gname == "bracketed":
                continue  # "(0917) 123-4567" style groups the trunk with the area code; covered by its own case below
            yield pytest.param(region, prefix + grouped, expected, id=f"{region}-{gname}-{pname}")


@pytest.mark.parametrize("region,written,expected", list(cases()))
def test_every_format_of_a_number_is_one_e164_contact_in_every_country_shape(region, written, expected):
    assert normalise_phone(written, region) == expected
    assert contact_key(written, region) == "phone:" + expected


@pytest.mark.parametrize("region", sorted(SHAPES))
def test_an_international_number_reads_the_same_whatever_the_default_region(region):
    expected = "+" + REGIONS[region].code + SHAPES[region]
    for home in REGIONS:
        assert normalise_phone("+" + REGIONS[region].code + " " + groupings(SHAPES[region])["spaced"], home) == expected


def test_the_two_spellings_the_brief_names_are_one_contact():
    assert contact_key("(555) 010-1234") == contact_key("+15550101234") == "phone:+15550101234"


def test_a_trunk_prefix_grouped_with_the_area_code_is_read():
    assert normalise_phone("(0917) 123-4567", "PH") == "+639171234567"


INVALID_PHONES = [
    ("", "empty"),
    ("   ", "empty"),
    (None, "empty"),
    (5550101234, "empty"),
    ("abc", "characters"),
    ("555-010-1234 ext 5", "characters"),
    ("555-0101", "valid US"),
    ("55501012345", "valid US"),
    ("0555010123", "never starts with 0"),
    ("++15550101234", "characters"),
    ("1+5550101234", "characters"),
    ("+1 555 010 12345", "+1 number"),
    ("+1 555 010 123", "+1 number"),
    ("+0123456789", "country code"),
    ("+1234", "8 to 15"),
    ("+1234567890123456", "8 to 15"),
    ("0", "valid US"),
    ("00", "country code"),
    ("+63 917 123 456", "+63 number"),
    ("+44 7911 12345", "+44 number"),
]


@pytest.mark.parametrize("raw,reason", INVALID_PHONES)
def test_a_number_that_is_not_one_is_refused_with_a_reason_and_never_a_guess(raw, reason):
    with pytest.raises(ContactError) as err:
        normalise_phone(raw)
    assert reason in str(err.value)
    shown = "".join(ch for ch in str(raw or "") if ch.isdigit())
    assert len(shown) < 7 or shown not in str(err.value)  # a reason never repeats the contact


def test_an_unknown_region_is_a_setup_error_not_an_invalid_row():
    with pytest.raises(ContactError, match="unknown region"):
        normalise_phone("5550101234", "ZZ")


@pytest.mark.parametrize("raw,key", [
    ("Pat@Example.COM", "pat@example.com"),
    ("  pat.lead+tag@Mail.Example.org ", "pat.lead+tag@mail.example.org"),
    ("a@b.co", "a@b.co"),
])
def test_an_email_is_lower_cased_and_trimmed(raw, key):
    assert normalise_email(raw) == key
    assert contact_key(raw) == "email:" + key


@pytest.mark.parametrize("raw", [
    "", "  ", None, 7, "pat@", "@example.com", "pat example@x.com", "pat@example", "pat@@example.com", "pat@example..com",
    "pat@.example.com", "pat@example.com.", "pat@x.com,lee@y.com", "pat@x.com;lee@y.com", "<pat@x.com>", "a" * 250 + "@x.com",
])
def test_a_string_that_is_not_an_address_is_refused(raw):
    with pytest.raises(ContactError):
        normalise_email(raw)


def test_an_email_and_a_phone_are_never_the_same_contact_and_an_at_sign_decides_which_it_is():
    assert contact_key("5550101234").startswith("phone:") and contact_key("5550101234@x.com").startswith("email:")
    assert contact_key("5550101234") != contact_key("5550101234@x.com")


def test_a_lead_is_known_by_its_email_and_its_phone_and_an_unreadable_value_is_left_out():
    assert lead_keys({"email": "Pat@X.com", "phone": "(555) 010-1234"}) == ["email:pat@x.com", "phone:+15550101234"]
    assert lead_keys({"email": "not an email", "phone": "(555) 010-1234"}) == ["phone:+15550101234"]
    assert lead_keys({"email": None, "phone": None}) == []
    assert lead_keys({}) == []
    assert lead_keys({"phone": "09171234567"}, "PH") == ["phone:+639171234567"]


def test_the_default_region_comes_from_the_environment_and_a_bad_one_is_refused():
    assert contacts.default_region({}) == "US"
    assert contacts.default_region({"RELAY_DEFAULT_REGION": " ph "}) == "PH"
    with pytest.raises(ContactError):
        contacts.default_region({"RELAY_DEFAULT_REGION": "ZZ"})


def test_seeded_property_formats_of_one_number_agree_idempotent_and_different_numbers_stay_apart():
    rng = random.Random(20261024)
    seen: dict[str, str] = {}
    for _ in range(600):
        region = rng.choice(sorted(SHAPES))
        r = REGIONS[region]
        national = str(rng.randint(2, 9)) + "".join(str(rng.randint(0, 9)) for _ in range(r.national_lengths[0] - 1))
        formats = list(itertools.product(groupings(national).values(), prefixes(region).values()))
        written = [p + g for g, p in rng.sample(formats, 3)]
        keys = {normalise_phone(w, region) for w in written}
        assert len(keys) == 1, (region, written)
        (key,) = keys
        assert normalise_phone(key, region) == key  # E.164 in, the same E.164 out
        assert normalise_phone(key, rng.choice(sorted(REGIONS))) == key  # and no region changes a number that carries its country
        assert seen.setdefault(key, national) == national  # two different numbers never share a key
