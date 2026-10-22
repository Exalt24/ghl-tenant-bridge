"""GoHighLevel specifics: the key that identifies a delivery, the lead shape the sinks read, and the Ed25519 check.

Evidence for every field below is in the research run's evidence/D-ghl-railway (marketplace webhook pages, saved
2026-10-22). Three facts shaped it:
  - The per-event schemas carry no webhookId. The envelope says it exists, the pages do not show it, so a delivery
    that arrives without a native id gets a SYNTHESISED key and the README says to confirm on a live delivery.
  - Contact and Opportunity events are flat (`id`, `dateAdded`, `dateUpdated` at the top). Appointment events nest
    them under `appointment`. Extraction is per type, and an unknown type falls back to hashing the whole body.
  - ContactCreate and ContactUpdate carry NO attribution. Attribution is read from a nested attributionSource object
    when a payload has one, and from flat workflow-body keys otherwise. Missing attribution stays missing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Published by HighLevel for X-GHL-Signature (Webhook Integration Guide). Base64 of the SPKI DER.
GHL_ED25519_SPKI_B64 = "MCowBQYDK2VwAyEAi2HR1srL4o18O8BRa7gVJY7G7bupbN3H9AwJrHCDiOg="

BASIS_NATIVE = "native"
BASIS_WORKFLOW_KEY = "workflow-key"
BASIS_BODY_HASH = "body-hash"
BASIS_SYNTHESISED = "synthesised"
DERIVED_BASES = (BASIS_WORKFLOW_KEY, BASIS_BODY_HASH, BASIS_SYNTHESISED)

# ASSUMED, from edge/src/core.ts: a delivery older than the 30 day dedupe retention is refused, and one more than five
# minutes ahead of the clock is refused. Off by default (see timestamp_ok).
MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000
MAX_FUTURE_MS = 5 * 60 * 1000


def sha256_hex(*parts: str | bytes) -> str:
    h = hashlib.sha256()
    for i, p in enumerate(parts):
        if i:
            h.update(b"|")
        h.update(p if isinstance(p, bytes) else p.encode("utf-8"))
    return h.hexdigest()


# ---- keys ---------------------------------------------------------------------------------------------------------


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _flat_identity(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    return _str(payload.get("id")), _str(payload.get("dateUpdated")) or _str(payload.get("dateAdded"))


def _appointment_identity(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    inner = payload.get("appointment")
    if not isinstance(inner, dict):
        return None, None
    return _str(inner.get("id")), _str(inner.get("dateUpdated")) or _str(inner.get("dateAdded"))


def identity_for(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """(entity id, stamp) by event type, or (None, None) when the type is unknown or the fields are missing."""
    kind = payload.get("type")
    if not isinstance(kind, str):
        return None, None
    if kind.startswith("Appointment"):
        return _appointment_identity(payload)
    if kind.startswith("Contact") or kind.startswith("Opportunity"):
        return _flat_identity(payload)
    return None, None


def derive_key(
    tenant: str,
    payload: dict[str, Any],
    raw: bytes,
    *,
    native_id: str | None = None,
    event_key: str | None = None,
) -> tuple[str, str]:
    """(key, basis). Never keyed on a contact id alone: two different events for one contact must not collide."""
    if native_id:
        return native_id, BASIS_NATIVE
    if event_key:
        return sha256_hex(tenant, event_key), BASIS_WORKFLOW_KEY
    entity_id, stamp = identity_for(payload)
    if entity_id and stamp:
        return sha256_hex(tenant, str(payload["type"]), entity_id, stamp, sha256_hex(raw)), BASIS_SYNTHESISED
    return sha256_hex(tenant, raw), BASIS_BODY_HASH


# ---- the lead the sinks read --------------------------------------------------------------------------------------

_ATTR_FLAT = {
    "utm_source": ("utm_source", "utmSource"),
    "utm_medium": ("utm_medium", "utmMedium"),
    "utm_campaign": ("utm_campaign", "campaign", "utmCampaign"),
    "utm_content": ("utm_content", "utmContent"),
    "utm_term": ("utm_term", "utmTerm", "utmKeyword"),
    "fbclid": ("fbclid",),
    "gclid": ("gclid",),
    "fbc": ("fbc",),
    "fbp": ("fbp",),
    "fb_event_id": ("fbEventId", "event_id"),
    "page_url": ("url", "page_url"),
    "referrer": ("referrer",),
    "campaign_id": ("campaignId",),
    "ip": ("ip",),
    "user_agent": ("userAgent", "user_agent"),
}

_KNOWN_TOP = {
    "type", "locationId", "id", "contactId", "contact_id", "name", "full_name", "firstName", "first_name", "lastName",
    "last_name", "email", "phone", "city", "state", "country", "postalCode", "postal_code", "source", "tags",
    "dateAdded", "dateUpdated", "monetaryValue", "status", "appointment", "attributionSource", "lastAttributionSource",
    "customFields", "address1", "companyName", "website", "assignedTo", "dnd", "dateOfBirth", "webhookId", "timestamp",
    "event_key", "additionalEmails", "additionalPhones", "phoneLabel", *{k for v in _ATTR_FLAT.values() for k in v},
}

MAX_FIELD_CHARS = 50_000
MAX_ANSWERS = 40
MAX_TAGS = 50


def _text(v: Any, limit: int = MAX_FIELD_CHARS) -> str | None:
    """A string, or a number as a string. Anything else (null, list, object, bool) is treated as absent."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        s = v.strip()
        return s[:limit] if s else None
    return None


def _first(d: dict[str, Any], *keys: str) -> str | None:
    for k in keys:
        t = _text(d.get(k))
        if t:
            return t
    return None


def normalise_lead(payload: Any) -> dict[str, Any]:
    """Coerces a delivery into the shape every sink reads. Never raises on odd field types: a malformed lead becomes a
    sparse one, because one bad lead must never stop the fan-out."""
    if not isinstance(payload, dict):
        return _empty_lead()
    if isinstance(payload.get("data"), dict):
        # The integration guide's envelope wraps the fields in `data`, the per event pages show them flat (ASSUMED which
        # one a live delivery uses). Read either: the envelope's own keys win over a field of the same name inside it.
        payload = {**payload["data"], **{k: v for k, v in payload.items() if k != "data"}}
    inner = payload.get("appointment") if isinstance(payload.get("appointment"), dict) else None
    src = inner if inner is not None else payload

    name = _first(payload, "name", "full_name")
    first = _first(payload, "firstName", "first_name")
    last = _first(payload, "lastName", "last_name")
    if not name and (first or last):
        name = " ".join(x for x in (first, last) if x)
    if not name and inner is not None:
        name = _first(inner, "title")

    tags_raw = payload.get("tags")
    tags = [t for t in (_text(x, 200) for x in tags_raw[:MAX_TAGS]) if t] if isinstance(tags_raw, list) else []

    attribution = _attribution(payload)
    answers: dict[str, str] = {}
    for k, v in payload.items():
        if len(answers) >= MAX_ANSWERS:
            break
        if k in _KNOWN_TOP or not isinstance(k, str) or len(k) > 64:
            continue
        t = _text(v)
        if t is not None:
            answers[k] = t

    monetary = payload.get("monetaryValue")
    return {
        "event_type": _text(payload.get("type"), 100),
        "location_id": _text(payload.get("locationId"), 100),
        "contact_id": _first(payload, "contactId", "contact_id") or (_first(inner, "contactId") if inner else None)
        or (_text(payload.get("id"), 100) if str(payload.get("type", "")).startswith("Contact") else None),
        "name": name,
        "first_name": first,
        "last_name": last,
        "email": _first(payload, "email"),
        "phone": _first(payload, "phone"),
        "city": _first(payload, "city"),
        "state": _first(payload, "state"),
        "country": _first(payload, "country"),
        "postal_code": _first(payload, "postalCode", "postal_code"),
        "source": _first(src, "source"),
        "tags": tags,
        "date_added": _first(src, "dateAdded"),
        "date_updated": _first(src, "dateUpdated"),
        "monetary_value": monetary if isinstance(monetary, (int, float)) and not isinstance(monetary, bool) else None,
        "status": _first(src, "status", "appointmentStatus"),
        "attribution": attribution,
        "answers": answers,
    }


def _empty_lead() -> dict[str, Any]:
    return {
        "event_type": None, "location_id": None, "contact_id": None, "name": None, "first_name": None, "last_name": None,
        "email": None, "phone": None, "city": None, "state": None, "country": None, "postal_code": None, "source": None,
        "tags": [], "date_added": None, "date_updated": None, "monetary_value": None, "status": None,
        "attribution": {**{k: None for k in _ATTR_FLAT}, "present": False}, "answers": {},
    }


def _attribution(payload: dict[str, Any]) -> dict[str, Any]:
    """Nested attributionSource first (Get Contact shape), then flat workflow keys. A value is never guessed."""
    out: dict[str, Any] = {k: None for k in _ATTR_FLAT}
    sources: list[dict[str, Any]] = []
    for name in ("attributionSource", "lastAttributionSource"):
        v = payload.get(name)
        if isinstance(v, dict):
            sources.append(v)
    sources.append(payload)
    for field, keys in _ATTR_FLAT.items():
        for s in sources:
            got = _first(s, *keys)
            if got:
                out[field] = got
                break
    out["present"] = any(v for v in out.values())
    return out


# ---- Ed25519 ------------------------------------------------------------------------------------------------------


def _b64decode(value: str) -> bytes | None:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def _load_key(spki_b64: str) -> Ed25519PublicKey:
    der = base64.b64decode(spki_b64)
    key = serialization.load_der_public_key(der)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("not an Ed25519 key")
    return key


def _check(key: Ed25519PublicKey, sig: bytes, message: bytes) -> bool:
    try:
        key.verify(sig, message)
        return True
    except InvalidSignature:
        return False


def _reserialise(raw: bytes) -> bytes | None:
    """The compact form JSON.stringify would produce. ASSUMED to be what HighLevel signs, because its own sample code
    verifies JSON.stringify(req.body); a live delivery settles which path is needed."""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_signature(raw: bytes, signature_b64: str | None, spki_b64: str = GHL_ED25519_SPKI_B64) -> str | None:
    """'raw' when the signature verifies over the bytes exactly as received, 'reserialised' when it verifies over the
    compact re-serialisation, None otherwise. The caller must still parse the SAME raw bytes afterwards."""
    if not signature_b64:
        return None
    sig = _b64decode(signature_b64)  # GHL sometimes sends the literal "N/A"; that is not valid base64 here
    if sig is None:
        return None
    key = _load_key(spki_b64)
    if _check(key, sig, raw):
        return "raw"
    reser = _reserialise(raw)
    if reser is not None and reser != raw and _check(key, sig, reser):
        return "reserialised"
    return None


def signature_header(headers: Any) -> str | None:
    """The X-GHL-Signature value. A request carrying ONLY the legacy X-WH-Signature is refused (announced for
    deprecation 2026-09-01, so presenting it now is a spoof of an old scheme): it yields None here."""
    return headers.get("X-GHL-Signature") or None


def timestamp_ok(value: Any, now_ms: float) -> bool:
    """The optional timestamp window (ASSUMED rule, copied from the Worker): not older than 30 days, not more than
    5 minutes ahead. Off unless a tenant enables it, because the meaning of `timestamp` on a retry is undocumented."""
    if not isinstance(value, str):
        return False
    try:
        sent = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    sent_ms = sent.timestamp() * 1000
    return not (now_ms - sent_ms > MAX_AGE_MS or sent_ms - now_ms > MAX_FUTURE_MS)
