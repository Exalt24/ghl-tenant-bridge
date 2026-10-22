"""Flask app: the ingress routes. /health, /ingest, /hooks/<tenant>/workflow, /webhooks/ghl and the two admin routes.

Rules every route follows:
  - Auth before anything else is read. Secrets are compared with hmac.compare_digest.
  - `request.get_data()` is called first, with a per route size cap (413 beyond it), and the SAME bytes are parsed.
  - Nothing is acknowledged before the durable commit. Any failure before the commit is a 503, never a 200 or 202, so
    the sender keeps the delivery and retries.
  - The request path only stores. Sending is the worker's job.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from . import fanout, fathom_hook, ghl, redact
from .config import Config, load_config
from .fanout import Unrouted
from .http import Http
from .store import open_store
from .store.base import SqlStore

__all__ = ["Deps", "create_app"]

log = logging.getLogger("relay.app")

MAX_KEY_CHARS = 128
MAX_EVENT_KEY_CHARS = 256


@dataclass
class Deps:
    store: SqlStore
    http: Any = None
    # Seconds since the epoch. Only the optional HighLevel timestamp window reads it; a test injects a fixed one.
    clock: Callable[[], float] = time.time


def _eq(a: str, b: str) -> bool:
    """Constant time. A plain == leaks how many leading characters of a secret were right."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _bearer(header: str | None) -> str | None:
    if not header or not header.lower().startswith("bearer "):
        return None
    return header[7:].strip() or None


def _any_match(presented: str | None, *expected: str | None) -> bool:
    """True if the presented secret equals any non empty expected one. Every candidate is compared, no early exit."""
    ok = False
    for e in expected:
        if e and presented is not None:
            ok |= _eq(presented, e)
    return ok


def _err(status: int, code: str) -> Response:
    resp = jsonify({"ok": False, "error": code})
    resp.status_code = status
    return resp


def create_app(cfg: Config | None = None, deps: Deps | None = None) -> Flask:
    cfg = cfg or load_config(role="web")
    redact.install()
    redact.register(*cfg.secret_values())
    if deps is None:
        deps = Deps(store=open_store(cfg.database_url, cfg.db_path), http=Http())
    store = deps.store
    app = Flask(__name__)
    app.config["RELAY_CFG"] = cfg
    app.config["RELAY_STORE"] = store

    def read_body(cap: int) -> bytes:
        request.max_content_length = cap
        return request.get_data()

    def alert_routing(tenant: str | None, reason: str) -> None:
        try:
            store.enqueue_alert("routing", tenant, "dead", reason)
        except Exception as exc:  # the alert is best effort, the 503 already tells the sender to retry
            log.error("could not queue routing alert: %s", type(exc).__name__)

    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(_e: Exception) -> Response:
        return _err(413, "body-too-large")

    @app.errorhandler(404)
    def _not_found(_e: Exception) -> Response:
        return _err(404, "not-found")

    @app.errorhandler(405)
    def _bad_method(_e: Exception) -> Response:
        return _err(405, "method-not-allowed")

    @app.get("/health")
    def health() -> Response:
        try:
            if not store.ping():
                return _err(503, "db")
            counts = store.health_counts()
        except Exception as exc:
            log.error("health check failed: %s", type(exc).__name__)
            return _err(503, "db")
        return jsonify({"ok": True, "db": "ok", **counts})

    @app.post("/ingest")
    def ingest_route() -> Response:
        presented = _bearer(request.headers.get("Authorization"))
        if not _any_match(presented, cfg.ingest_token, cfg.ingest_token_previous):
            return _err(401, "unauthorised")
        raw = read_body(cfg.max_body_bytes)
        key = request.headers.get("Idempotency-Key", "")
        if not key or len(key) > MAX_KEY_CHARS or not key.isprintable():
            return _err(400, "idempotency-key-required")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return _err(400, "not-json")
        if not isinstance(payload, dict):
            return _err(400, "not-an-object")
        webhook_id = payload.get("webhookId")
        if webhook_id is not None and webhook_id != key:
            return _err(400, "webhook-id-mismatch")
        try:
            tenant = fanout.route(cfg, payload)
            result = fanout.ingest(
                store, cfg, tenant=tenant, key=key, basis=ghl.BASIS_NATIVE, source="ingest", payload_json=raw.decode("utf-8")
            )
        except Unrouted as exc:
            alert_routing(cfg.tenant_for_location(payload.get("locationId")), str(exc))
            return _err(503, "unrouted")
        except Exception as exc:
            log.error("ingest failed before commit: %s", type(exc).__name__)
            return _err(503, "store")
        return jsonify({"ok": True, "duplicate": result.duplicate, "event_id": result.event_id})

    @app.post("/hooks/<tenant>/workflow")
    def workflow_route(tenant: str) -> Response:
        tcfg = cfg.tenants.get(tenant)
        presented = request.headers.get("X-Relay-Secret")
        expected = tcfg.workflow_secret if tcfg else None
        if not _any_match(presented, expected):
            return _err(401, "unauthorised")
        assert tcfg is not None
        raw = read_body(tcfg.max_body_bytes)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return _err(400, "not-json")
        if not isinstance(payload, dict):
            return _err(400, "not-an-object")
        event_key = payload.get("event_key")
        if event_key is not None and (not isinstance(event_key, str) or not event_key or len(event_key) > MAX_EVENT_KEY_CHARS):
            return _err(400, "bad-event-key")
        if event_key is None and tcfg.dedupe_mode != "body_hash":
            return _err(400, "event_key-required: put a per-submission token in the workflow body as event_key")
        try:
            key, basis = ghl.derive_key(tenant, payload, raw, event_key=event_key)
            result = fanout.ingest(
                store, cfg, tenant=tenant, key=key, basis=basis, source="workflow", payload_json=raw.decode("utf-8")
            )
        except Unrouted as exc:
            alert_routing(tenant, str(exc))
            return _err(503, "unrouted")
        except Exception as exc:
            log.error("workflow ingest failed before commit: %s", type(exc).__name__)
            return _err(503, "store")
        return jsonify({"ok": True, "duplicate": result.duplicate, "event_id": result.event_id})

    @app.post("/webhooks/ghl")
    def ghl_route() -> Response:
        """The direct HighLevel marketplace webhook. The signature is checked over the exact bytes received (or over the
        compact re-serialisation, because HighLevel's own sample verifies JSON.stringify(req.body) and which one a live
        delivery needs is not known), and only then are the SAME bytes parsed."""
        raw = read_body(cfg.ghl_webhook_max_body_bytes)
        signature = ghl.signature_header(request.headers)
        if signature is None:
            # A request carrying ONLY the legacy X-WH-Signature is refused: announced for deprecation on 2026-09-01.
            return _err(401, "legacy-signature-refused" if request.headers.get("X-WH-Signature") else "unauthorised")
        try:
            how = ghl.verify_signature(raw, signature, cfg.ghl_public_key_b64)
        except Exception as exc:  # a malformed configured key is our fault, not the sender's
            log.error("signature check could not run: %s", type(exc).__name__)
            return _err(503, "signature-check")
        if how is None:
            return _err(401, "unauthorised")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return _err(400, "not-json")
        if not isinstance(payload, dict):
            return _err(400, "not-an-object")
        try:
            tenant = fanout.route(cfg, payload)
        except Unrouted as exc:
            alert_routing(cfg.tenant_for_location(payload.get("locationId")), str(exc))
            return _err(503, "unrouted")
        if cfg.tenants[tenant].ghl_timestamp_window and not ghl.timestamp_ok(payload.get("timestamp"), deps.clock() * 1000):
            return _err(401, "timestamp-outside-window")
        webhook_id = payload.get("webhookId")
        native = webhook_id if isinstance(webhook_id, str) and 0 < len(webhook_id) <= MAX_KEY_CHARS else None
        try:
            key, basis = ghl.derive_key(tenant, payload, raw, native_id=native)
            result = fanout.ingest(
                store, cfg, tenant=tenant, key=key, basis=basis, source="ghl-direct", payload_json=raw.decode("utf-8")
            )
        except Unrouted as exc:
            alert_routing(tenant, str(exc))
            return _err(503, "unrouted")
        except Exception as exc:
            log.error("direct webhook ingest failed before commit: %s", type(exc).__name__)
            return _err(503, "store")
        return jsonify({"ok": True, "duplicate": result.duplicate, "event_id": result.event_id})

    @app.post("/webhooks/fathom/<tenant>")
    def fathom_route(tenant: str) -> Response:
        """A Fathom webhook (Standard Webhooks). Verified over the raw bytes first; the meeting is stored in the SAME
        transaction as its event, keyed on `webhook-id` (Fathom keeps it the same when it resends), so a retry is a
        suppressed duplicate and a crash can never leave one without the other."""
        tcfg = cfg.tenants.get(tenant)
        fcfg = tcfg.fathom if tcfg else None
        if fcfg is None:
            return _err(401, "unauthorised")  # an unknown tenant answers like a bad signature, so the route names no tenant
        raw = read_body(fcfg.max_body_bytes)
        problem = fathom_hook.verify(fathom_hook.parse_secret(fcfg.secret), request.headers, raw, deps.clock(), fcfg.tolerance_s)
        if problem is not None:
            return _err(401, problem)
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return _err(400, "not-json")
        meeting = fathom_hook.normalise_meeting(doc)
        if meeting is None:
            return _err(400, "recording_id-required")
        message_id = request.headers["webhook-id"]
        sinks = ["slack"] if fcfg.slack else []

        def keep_meeting(t: Any) -> None:
            store.save_meeting(
                t, tenant=tenant, recording_id=meeting["recording_id"], title=meeting["title"], summary=meeting["summary"],
                action_items=fathom_hook.action_items_text(meeting),
            )

        try:
            result = store.ingest(
                key=message_id, tenant=tenant, source="fathom", basis=ghl.BASIS_NATIVE,
                payload=fathom_hook.build_reduced_payload(meeting), sinks=sinks, window_s=None, on_new=keep_meeting,
            )
        except Exception as exc:
            log.error("fathom ingest failed before commit: %s", type(exc).__name__)
            return _err(503, "store")
        return jsonify({"ok": True, "duplicate": result.duplicate, "event_id": result.event_id})

    def admin_gate() -> Response | None:
        if not cfg.admin_token:
            return _err(404, "not-found")
        if not _any_match(_bearer(request.headers.get("Authorization")), cfg.admin_token):
            return _err(401, "unauthorised")
        return None

    def admin_json() -> dict[str, Any] | Response:
        raw = read_body(cfg.max_body_bytes)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return _err(400, "not-json")
        return body if isinstance(body, dict) else _err(400, "not-an-object")

    @app.post("/admin/replay")
    def replay_route() -> Response:
        denied = admin_gate()
        if denied is not None:
            return denied
        body = admin_json()
        if isinstance(body, Response):
            return body
        event_id, sink, force = body.get("event_id"), body.get("sink"), body.get("force", False)
        if not isinstance(event_id, str) or not event_id:
            return _err(400, "event_id-required")
        if sink is not None and not isinstance(sink, str):
            return _err(400, "bad-sink")
        if force is not True and force is not False:
            return _err(400, "bad-force")
        if force and not sink:
            return _err(400, "force-needs-sink")
        try:
            if store.get_event(event_id) is None:
                return _err(404, "unknown-event")
            n = store.replay(event_id, sink, force)
        except Exception as exc:
            log.error("replay failed: %s", type(exc).__name__)
            return _err(503, "store")
        return jsonify({"ok": True, "reset": n})

    @app.post("/admin/backfill")
    def backfill_route() -> Response:
        denied = admin_gate()
        if denied is not None:
            return denied
        body = admin_json()
        if isinstance(body, Response):
            return body
        tenant, sink, since = body.get("tenant"), body.get("sink"), body.get("since")
        tcfg = cfg.tenants.get(tenant) if isinstance(tenant, str) else None
        if tcfg is None:
            return _err(400, "unknown-tenant")
        scfg = tcfg.sinks.get(sink) if isinstance(sink, str) else None
        if scfg is None or not scfg.enabled:
            return _err(400, "sink-not-enabled-for-tenant")
        try:
            when = datetime.fromisoformat(since.replace("Z", "+00:00")) if isinstance(since, str) else None
        except ValueError:
            when = None
        if when is None:
            return _err(400, "since-must-be-iso-8601")
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        try:
            n = store.backfill(tenant, sink, when)
        except Exception as exc:
            log.error("backfill failed: %s", type(exc).__name__)
            return _err(503, "store")
        return jsonify({"ok": True, "inserted": n})

    if cfg.inline_worker and deps.http is not None:
        _start_inline_worker(app, cfg, store, deps.http)
    return app


def _start_inline_worker(app: Flask, cfg: Config, store: SqlStore, http: Any) -> None:
    """For the single container SQLite deployment: the worker runs as a thread beside the web process."""
    from .worker import Worker

    worker = Worker(cfg, store, http)
    thread = threading.Thread(target=worker.loop, args=(cfg.worker_interval_s,), name="relay-inline-worker", daemon=True)
    thread.start()
    app.config["RELAY_INLINE_WORKER"] = worker
