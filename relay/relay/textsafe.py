"""Text a sink can always encode.

A JSON escape such as "\\ud83d" is a lone half of an emoji (a form field cut in the middle of a character). It parses into a
Python string that cannot be encoded as UTF-8, so every sink that builds its request body with `.encode("utf-8")` died on it
with `sink bug: UnicodeEncodeError`. The relay replaces such a half with U+FFFD, the replacement character, at the two places
it builds a record from input: the canonical lead (`ghl.normalise_lead`), the stored payload as the sinks receive it
(`fanout._attempt`) and the canonical payment row (`reconcile.canonical`). Nothing after those places sees one.
"""

from __future__ import annotations

import re
from typing import Any

REPLACEMENT = "�"
# json.loads joins a valid escaped pair into one character, so any surrogate code point left in a string is a lone half.
_LONE = re.compile("[\ud800-\udfff]")


def clean_text(text: str) -> str:
    """The string with every lone surrogate replaced by U+FFFD. Any other character is untouched."""
    return _LONE.sub(REPLACEMENT, text) if not text.isascii() else text


def scrub(value: Any) -> Any:
    """Cleans every string in a parsed JSON value, keys included, IN PLACE for a dict or a list, and returns it. Iterative,
    so a deeply nested body cannot run out of stack here."""
    if isinstance(value, str):
        return clean_text(value)
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in list(node):
                item = node[key]
                if isinstance(key, str):
                    fixed = clean_text(key)
                    if fixed != key:
                        del node[key]
                        node[fixed] = item
                        key = fixed
                if isinstance(item, str):
                    node[key] = clean_text(item)
                elif isinstance(item, (dict, list)):
                    stack.append(item)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                if isinstance(item, str):
                    node[i] = clean_text(item)
                elif isinstance(item, (dict, list)):
                    stack.append(item)
    return value
