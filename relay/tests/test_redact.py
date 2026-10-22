"""The redaction mask, as a seeded property test.

Dimensions varied together: how many secrets are registered and whether they overlap or contain one another, where a
secret sits in the text (start, middle, end, repeated, adjacent to another, inside a URL query, inside a Bearer header),
look-alikes that are NOT secrets (a name that merely contains `token`, a short value, a value one character off), and
registration happening over time between calls.

Rules: no registered secret survives; masking twice equals masking once; text with nothing to hide is returned
unchanged; a look-alike is never masked.
"""

from __future__ import annotations

import random
import string

import pytest

from relay import redact

ALPHABET = string.ascii_letters + string.digits + "-_./:"
BENIGN = string.ascii_lowercase + " ,.;"  # no digits, no uppercase: can never form a generated secret


def secret(rng: random.Random, n: int | None = None) -> str:
    return "S" + "".join(rng.choice(ALPHABET) for _ in range(n if n is not None else rng.randint(6, 30)))


def benign(rng: random.Random, n: int | None = None) -> str:
    return "".join(rng.choice(BENIGN) for _ in range(n if n is not None else rng.randint(0, 25)))


@pytest.mark.parametrize("seed", range(40))
def test_masking_properties_over_random_worlds(seed):
    rng = random.Random(seed)
    redact.forget_all()
    registered: list[str] = []
    for step in range(25):
        # secrets enter over time, some overlapping: a longer one that contains a shorter one, or a shared prefix
        if rng.random() < 0.5:
            if registered and rng.random() < 0.4:
                base = rng.choice(registered)
                new = base + secret(rng, rng.randint(1, 8)) if rng.random() < 0.5 else secret(rng, 3) + base
            else:
                new = secret(rng)
            redact.register(new)
            registered.append(new)
        pieces: list[str] = []
        for _ in range(rng.randint(1, 8)):
            kind = rng.random()
            if registered and kind < 0.45:
                s = rng.choice(registered)
                pieces.append(rng.choice([s, f"url=https://h.test/{s}/x", f"?api_key={s}&z=1", f"Bearer {s}", f"{s}{s}", f'"{s}"']))
            elif kind < 0.6:
                pieces.append(f"?mytoken={benign(rng, 6) or 'x'}")  # contains the word token but is not one of the three names
            elif kind < 0.7:
                pieces.append(secret(rng, rng.randint(1, 3)))  # shorter than the minimum, never registered
            else:
                pieces.append(benign(rng))
        text = " ".join(pieces)
        masked = redact.mask(text)
        for s in registered:
            assert s not in masked, (seed, step, s)
        assert redact.mask(masked) == masked, (seed, step)  # idempotent


@pytest.mark.parametrize("seed", range(10))
def test_text_with_nothing_to_hide_is_returned_unchanged(seed):
    rng = random.Random(1000 + seed)
    redact.forget_all()
    for _ in range(5):
        redact.register(secret(rng))
    for _ in range(50):
        text = benign(rng, rng.randint(0, 80))
        assert redact.mask(text) == text


def test_longer_overlapping_secrets_are_masked_whole_not_leaving_a_tail():
    redact.register("abcdwxyz", "abcdwxyz-and-more")
    assert redact.mask("see abcdwxyz-and-more now") == "see *** now"
    assert redact.mask("see abcdwxyz now") == "see *** now"


def test_the_three_query_names_are_masked_and_look_alikes_are_not():
    for name in ("api_key", "access_token", "token", "TOKEN", "Api_Key"):
        assert redact.mask(f"https://x.test/p?{name}=VALUE123&k=v") == f"https://x.test/p?{name}=***&k=v"
    assert redact.mask("https://x.test/p?mytoken=VALUE123") == "https://x.test/p?mytoken=VALUE123"
    assert redact.mask("https://x.test/p?token_type=bearer") == "https://x.test/p?token_type=bearer"
    assert redact.mask("https://x.test/p?a=1&token=VALUE123") == "https://x.test/p?a=1&token=***"


def test_a_short_value_is_never_registered_so_it_cannot_blank_ordinary_text():
    redact.register("ab", "xyz", "", None)
    assert redact.mask("the abc and xyz are fine") == "the abc and xyz are fine"
    redact.register("abcd")
    assert redact.mask("the abcd is masked") == "the *** is masked"


def test_registering_after_a_line_was_built_still_masks_the_next_call():
    redact.forget_all()
    line = "token is LATECOMER-secret-1234 here"
    assert redact.mask(line) == line
    redact.register("LATECOMER-secret-1234")
    assert redact.mask(line) == "token is *** here"


def test_install_is_idempotent_and_uninstall_restores_the_original_factory():
    import logging

    before = logging.getLogRecordFactory()
    redact.uninstall()
    redact.install()
    installed = logging.getLogRecordFactory()
    assert installed is not before
    redact.install()
    assert logging.getLogRecordFactory() is installed  # a second install does not wrap again
    redact.uninstall()
    assert logging.getLogRecordFactory() is not installed
    redact.install()  # leave it installed for the rest of the session, as the app does
