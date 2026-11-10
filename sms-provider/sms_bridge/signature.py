"""HighLevel's Ed25519 signature on a Delivery URL post: the X-GHL-Signature header, checked over the exact raw bytes.

Same approach as the Worker in edge/src/core.ts and the relay in relay/relay/ghl.py: the published key is the base64 of its
SPKI DER, the header is base64 of the 64 byte signature, the legacy X-WH-Signature header (RSA) is never accepted, and a
header that is missing, not base64 or not a valid signature is a failure. The verification itself is the `cryptography`
package's, which compares in constant time; there is no curve code here.
"""

from __future__ import annotations

import base64
import binascii

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Published by HighLevel for X-GHL-Signature (Webhook Integration Guide). Base64 of the SPKI DER. Same value as
# GHL_ED25519_SPKI_B64 in edge/src/core.ts and relay/relay/ghl.py.
GHL_ED25519_SPKI_B64 = "MCowBQYDK2VwAyEAi2HR1srL4o18O8BRa7gVJY7G7bupbN3H9AwJrHCDiOg="

OK = "ok"
MISSING = "no X-GHL-Signature header"
LEGACY_ONLY = "only the legacy X-WH-Signature header"
BAD = "signature does not match the body"


def load_key(spki_b64: str) -> Ed25519PublicKey:
    """Raises ValueError for text that is not the base64 of an Ed25519 SPKI public key."""
    try:
        key = serialization.load_der_public_key(base64.b64decode(spki_b64, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not a base64 SPKI public key") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("not an Ed25519 key")
    return key


def check(headers, raw: bytes, key: Ed25519PublicKey) -> str:
    """OK, or the reason the post is not proven to come from HighLevel. `raw` is the body exactly as received."""
    value = headers.get("X-GHL-Signature")
    if not value:
        return LEGACY_ONLY if headers.get("X-WH-Signature") else MISSING
    try:
        sig = base64.b64decode(value, validate=True)  # HighLevel sometimes sends the literal "N/A"; that is not base64 here
    except (binascii.Error, ValueError):
        return BAD
    try:
        key.verify(sig, raw)
    except InvalidSignature:
        return BAD
    return OK
