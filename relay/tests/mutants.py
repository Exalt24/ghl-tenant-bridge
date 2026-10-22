"""Mutation check for relay/.

Each mutant copies the package, changes ONE guard (a text edit whose pattern must occur exactly once), runs the suite,
and requires a non-zero exit. A mutant that survives means the guard has no test that notices its absence, so fix the
test, never the mutant.

    python tests/mutants.py              run all mutants (4 in parallel)
    python tests/mutants.py --list       print the names
    python tests/mutants.py --check      only verify every pattern occurs exactly once
    python tests/mutants.py --only slack run the mutants whose name or file contains the text

A mutant first runs the test files most likely to catch it. If those stay green it runs the whole suite before it is
called a survivor. Every mutated tree must still import and collect, so a mutant cannot be "caught" by a syntax error.
Postgres only mutants run against DATABASE_URL_TEST; without it they are listed as NOT RUN and the harness exits 2.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

RELAY = Path(__file__).resolve().parent.parent
REPO = RELAY.parent
IGNORE = shutil.ignore_patterns(".venv", "__pycache__", "*.pyc", ".pytest_cache", "*.db", "*.db-wal", "*.db-shm")
EXTRA_FROM_REPO = [".github/workflows/ci.yml", ".github/dependabot.yml", "CLAUDE.md", "README.md"]
RUN_TIMEOUT_S = 420
# Too slow to run for every mutant; the full suite (and the fallback below) still runs them.
SLOW_TESTS = ["tests/test_fanout.py::test_many_leads_against_a_rate_limiting_vendor_all_arrive_and_none_die"]
COLLECTION_ERROR = re.compile(r"errors? during collection|ERROR collecting", re.IGNORECASE)


@dataclass
class Mutant:
    name: str
    file: str
    edits: list[tuple[str, str]]
    tests: list[str] = field(default_factory=list)
    stores: str = "sqlite"


def M(name: str, file: str, *edits: tuple[str, str], tests: list[str] | None = None, stores: str = "sqlite") -> Mutant:
    return Mutant(name, file, list(edits), tests or [], stores)


STORE = "relay/store/base.py"
SQLITE = "relay/store/sqlite.py"
PG = "relay/store/postgres.py"
FAN = "relay/fanout.py"
APP = "relay/app.py"
CFG = "relay/config.py"
GHL = "relay/ghl.py"
SLK = "relay/sinks/slack.py"
MKE = "relay/sinks/make.py"
RED = "relay/redact.py"
WRK = "relay/worker.py"
ALR = "relay/alerts.py"
HTP = "relay/http.py"
FSLACK = "tests/fakes/slack.py"

T_STORE = ["tests/test_store_contract.py"]
T_FAN = ["tests/test_fanout.py"]
T_APP = ["tests/test_app.py"]
T_CFG = ["tests/test_config.py"]
T_GHL = ["tests/test_ghl.py"]
T_SINK = ["tests/test_sinks.py"]
T_CRASH = ["tests/test_crash_and_concurrency.py"]
T_SEC = ["tests/test_secrets.py", "tests/test_redact.py"]

MUTANTS: list[Mutant] = [
    # ---- ingest and the store ----------------------------------------------------------------------------------------
    M("delivery rows inserted outside the ingest transaction", STORE,
      ("            self._add_deliveries(t, event_id, tenant, sinks)\n            return IngestResult(event_id, False)",
       "            result = IngestResult(event_id, False)\n        with self.tx() as t2:\n            self._add_deliveries(t2, event_id, tenant, sinks)\n        return result"),
      tests=T_STORE),
    M("ON CONFLICT DO NOTHING removed from the event insert", STORE,
      ('f"VALUES (?, ?, ?, ?, ?, ?, {self.now()}, ?) ON CONFLICT (event_id) DO NOTHING",', 'f"VALUES (?, ?, ?, ?, ?, ?, {self.now()}, ?)",'),
      tests=T_STORE),
    M("dedupe window ignored", STORE,
      ('                    if float(last["age_s"]) < window_s:', "                    if False:"), tests=T_STORE),
    M("a conflicting insert is not recorded as suppressed", STORE,
      ("                self._suppress(t, event_id, basis, tenant)\n                return IngestResult(event_id, True)", "                return IngestResult(event_id, True)"),
      tests=T_STORE),
    M("claim lease ignored", STORE,
      ("f\"AND ({a}.claim_until IS NULL OR {a}.claim_until <= {n}) \"\n", "\"\"\n"), tests=T_STORE),
    M("claim takes more than one row", STORE,
      ("ORDER BY d.next_at, d.created_at, d.event_id LIMIT 1 {self.skip_locked})", "ORDER BY d.next_at, d.created_at, d.event_id LIMIT 3 {self.skip_locked})"),
      tests=T_STORE),
    M("claim token check removed from the final write", STORE,
      ("f\"WHERE event_id = ? AND sink = ? AND claim_token = ? AND state = 'pending'\",", "f\"WHERE event_id = ? AND sink = ? AND state = 'pending'\","),
      ("                    claim.sink,\n                    claim.token,\n", "                    claim.sink,\n"),
      tests=T_STORE),
    M("state change and attempt row written in two transactions", STORE,
      ('            self._fault("finish_mid")\n            t.execute(\n                f"INSERT INTO attempts',
       '        self._fault("finish_mid")\n        with self.tx() as t:\n            t.execute(\n                f"INSERT INTO attempts'),
      tests=T_STORE),
    M("lease taken from the process clock instead of the database clock", STORE,
      ('f"UPDATE deliveries SET claim_until = {self.now(lease_s)}, claim_token = ? "',
       "f\"UPDATE deliveries SET claim_until = '{__import__('datetime').datetime.fromtimestamp(__import__('time').time() + lease_s, __import__('datetime').timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}', claim_token = ? \""),
      tests=T_STORE),
    M("a pause does not hold claims back", STORE,
      ("AND (s.paused_until > {n} OR s.breaker_until > {n} OR s.next_send_at > {n})", "AND (s.breaker_until > {n} OR s.next_send_at > {n})"),
      tests=T_STORE),
    M("the circuit breaker does not hold claims back", STORE,
      ("AND (s.paused_until > {n} OR s.breaker_until > {n} OR s.next_send_at > {n})", "AND (s.paused_until > {n} OR s.next_send_at > {n})"),
      tests=T_STORE),
    M("the minimum interval does not hold claims back", STORE,
      ("AND (s.paused_until > {n} OR s.breaker_until > {n} OR s.next_send_at > {n})", "AND (s.paused_until > {n} OR s.breaker_until > {n})"),
      tests=T_STORE),
    M("breaker opens one failure late", STORE,
      ("CASE WHEN consecutive_failures + 1 >= ? THEN", "CASE WHEN consecutive_failures + 1 > ? THEN"), tests=T_STORE),
    M("expire_old kills rows younger than the limit", STORE,
      ("AND created_at <= {self.now(-max_age)} RETURNING", "AND created_at <= {self.now(max_age)} RETURNING"), tests=T_STORE),
    M("expire_old kills a row a worker still holds", STORE,
      ("f\"WHERE sink = ? AND state = 'pending' AND (claim_until IS NULL OR claim_until <= {self.now()}) \"", "f\"WHERE sink = ? AND state = 'pending' \""),
      tests=T_STORE),
    M("replay resets done rows without force", STORE,
      ('        states = ("dead", "done", "skipped") if force else ("dead",)', '        states = ("dead", "done", "skipped")'), tests=T_STORE),
    M("replay keeps the old age clock", STORE,
      ("f\"UPDATE deliveries SET state = 'pending', attempts = 0, retries = 0, created_at = {self.now()}, \"", "f\"UPDATE deliveries SET state = 'pending', attempts = 0, retries = 0, \""),
      tests=T_STORE),
    M("backfill duplicates existing delivery rows", STORE,
      ("f\"AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.event_id = e.event_id AND d.sink = ?)\",\n                (sink, tenant, self.ts_param(since), sink),",
       "f\"AND ? IS NOT NULL\",\n                (sink, tenant, self.ts_param(since), sink),"),
      tests=T_STORE),
    M("purge nulls payloads that are not old yet", STORE,
      ("AND received_at <= {self.now(-older_than_days * 86400.0)}", "AND received_at >= {self.now(-older_than_days * 86400.0)}"), tests=T_STORE),
    M("alert gate lets every caller through", STORE,
      ("WHERE sink = ? AND (last_sent_at IS NULL OR last_sent_at <= {self.now(-interval_s)})", "WHERE sink = ?"), tests=T_STORE),
    M("alert queue is not capped", STORE,
      ("WHERE (SELECT COUNT(*) FROM alerts WHERE sink = ? AND sent_at IS NULL) < {ALERT_CAP_PER_SINK}", "WHERE ? IS NOT NULL"),
      ("            (sink, tenant, kind, reason, event_id, sink),", "            (sink, tenant, kind, reason, event_id, sink),"), tests=T_STORE),
    M("Postgres claim does not skip locked rows", PG,
      ('    skip_locked = "FOR UPDATE SKIP LOCKED"', '    skip_locked = ""'), tests=T_STORE, stores="postgres"),
    # ---- fan-out ---------------------------------------------------------------------------------------------------------
    M("a retry is counted as an attempt", FAN,
      ("    if kind == RETRY:\n        plan.retries_inc = 1\n", "    if kind == RETRY:\n        plan.retries_inc = 1\n        plan.attempts_inc = 1\n"), tests=T_FAN),
    M("the attempt budget is never enforced", FAN,
      ("    if claim.attempts + plan.attempts_inc >= MAX_ATTEMPTS:", "    if False:"), tests=T_FAN),
    M("an ambiguous try does not spend the budget", FAN,
      ("    elif kind == AMBIGUOUS:\n        plan.attempts_inc = 1\n", "    elif kind == AMBIGUOUS:\n        plan.attempts_inc = 0\n"), tests=T_FAN),
    M("run_one does not check the age of a claimed row", FAN,
      ("    elif claim.age_s > scfg.max_age_s:", "    elif False:"), tests=T_FAN),
    M("a delivery that died without a try spends an attempt", FAN,
      ('        plan.state, plan.attempts_inc = "dead", 1 if sent else 0', '        plan.state, plan.attempts_inc = "dead", 1'), tests=T_FAN),
    M("a disabled sink raises instead of going dead", FAN,
      ("    if scfg is None or sink is None:", "    if False:"), tests=T_FAN),
    M("the catch-all around a sink is removed", FAN,
      ("    except Exception as exc:\n        log.error(\"sink %s raised %s\"", "    except ZeroDivisionError as exc:\n        log.error(\"sink %s raised %s\""), tests=T_FAN),
    M("the catch-all stores str(exc)", FAN,
      ('Outcome.dead(f"sink bug: {type(exc).__name__}")', 'Outcome.dead(f"sink bug: {exc}")'), tests=T_FAN + T_SEC),
    M("rate limiting sleeps instead of setting next_send_at", FAN,
      ("    plan = plan_for(outcome, claim, scfg, ctx.rng, sent)\n", "    plan = plan_for(outcome, claim, scfg, ctx.rng, sent)\n    if plan.min_interval_s:\n        __import__('time').sleep(plan.min_interval_s)\n"),
      tests=T_FAN),
    M("a pause does not pause", FAN,
      ("        plan.delay_s, plan.pause_s, plan.pause_reason = pause_s, pause_s, reason\n", "        plan.delay_s = 0.0\n"), tests=T_FAN),
    M("a 429 holds back only its own row", FAN,
      ("        if outcome.rate_limited:\n            plan.rate_limit_s = plan.delay_s\n", ""), tests=T_FAN),
    M("failures do not count toward the circuit breaker", FAN,
      ('    if outcome.breaker_failure:\n        plan.breaker = "fail"\n', ""), tests=T_FAN),
    M("a success does not close the breaker", FAN,
      ('        plan.breaker = "ok" if sent else None', "        plan.breaker = None"), tests=T_FAN),
    M("an ambiguous send on a sink that can read back is not reconciled", FAN,
      ("        if outcome.kind == AMBIGUOUS and sink.can_reconcile:", "        if False:"), tests=T_FAN),
    M("a skipped delivery raises an alert", FAN,
      ('    if kind == SKIPPED:\n        plan.state = "skipped"\n        return plan', '    if kind == SKIPPED:\n        plan.state = "skipped"\n        plan.alerts.append(("dead", "skipped"))\n        return plan'),
      tests=T_FAN),
    M("the retry backoff has no cap", FAN,
      ("    base = min(BACKOFF_BASE_S * (2 ** max(n, 0)), BACKOFF_CAP_S)", "    base = BACKOFF_BASE_S * (2 ** max(n, 0))"), tests=T_FAN),
    M("the lease is shorter than the sink timeout allows", FAN,
      ("    return max(LEASE_MIN_S, LEASE_TIMEOUT_FACTOR * max(timeouts))", "    return 1.0"), tests=T_FAN + T_CRASH),
    # ---- worker and alerts ---------------------------------------------------------------------------------------------------
    M("one sink's database error is not contained", WRK,
      ("        except Exception as exc:  # a database error on one sink must not stop the others", "        except ZeroDivisionError as exc:  # a database error on one sink must not stop the others"),
      tests=T_FAN),
    M("the wait budget of a one-shot run is removed", WRK,
      ("                if waited + wait > max_total_wait_s:", "                if False:"), tests=["tests/test_worker.py"]),
    M("sinks wait for each other (a barrier between sinks)", WRK,
      ("            total = sum(pool.map(lambda s: self._drain_sink(s, wait_horizon_s, max_total_wait_s), sinks))",
       "            total = 0\n            for s in sinks:\n                total += pool.submit(self._drain_sink, s, wait_horizon_s, max_total_wait_s).result()"),
      tests=T_FAN),
    M("a config error makes the worker exit 0", WRK, ("        return 2\n    redact.install()", "        return 0\n    redact.install()"), tests=["tests/test_worker.py"]),
    M("the alert digest is not rate limited", ALR,
      ("        if not store.alert_gate(sink, interval_s):\n            continue\n", ""), tests=T_FAN),
    M("a failed digest is marked sent", ALR, ("        if ok:\n            store.alerts_mark_sent", "        if True:\n            store.alerts_mark_sent"), tests=T_FAN),
    # ---- sinks -------------------------------------------------------------------------------------------------------------------
    M("Slack 5xx treated as dead instead of ambiguous", SLK,
      ('        if status >= 500:\n            return Outcome.ambiguous(status=status, reason=f"http {status}")', '        if status >= 500:\n            return Outcome.dead(f"http {status}", status=status)'),
      tests=T_SINK),
    M("Slack timeout treated as dead instead of ambiguous", SLK,
      ("        except (HttpTimeout, HttpFailure) as exc:\n            return Outcome.ambiguous(reason=exc.kind)", "        except (HttpTimeout, HttpFailure) as exc:\n            return Outcome.dead(exc.kind)"),
      tests=T_SINK),
    M("Make 5xx treated as dead instead of ambiguous", MKE,
      ('        if status >= 500:\n            return Outcome.ambiguous(status=status, reason=f"http {status}")', '        if status >= 500:\n            return Outcome.dead(f"http {status}", status=status)'),
      tests=T_SINK),
    M("Make timeout treated as dead instead of ambiguous", MKE,
      ("        except (HttpTimeout, HttpFailure) as exc:\n            return Outcome.ambiguous(reason=exc.kind)", "        except (HttpTimeout, HttpFailure) as exc:\n            return Outcome.dead(exc.kind)"),
      tests=T_SINK),
    M("Slack text is not escaped", SLK,
      ('    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")', "    return text"), tests=T_SINK),
    M("Slack text is truncated before escaping", SLK,
      ("    escaped = escape(text)\n    if len(escaped) <= limit:", "    escaped = escape(text[:limit])\n    if len(escaped) <= limit:"), tests=T_SINK),
    M("Slack truncation can leave half an entity", SLK,
      ("    if amp != -1 and \";\" not in text[amp:]:\n        return text[:amp]\n    return text", "    return text"), tests=T_SINK),
    M("Slack verbatim flag dropped", SLK,
      ('    return {"type": "mrkdwn", "text": text, "verbatim": True}', '    return {"type": "mrkdwn", "text": text}'), tests=T_SINK),
    M("Slack footer shows more than 8 characters of the event id", SLK,
      ("event `{event.event_id[:8]}`", "event `{event.event_id[:12]}`"), tests=T_SINK),
    M("Slack header is not capped at 150 characters", SLK,
      ("    if len(header) > MAX_HEADER_CHARS:", "    if False:"), tests=T_SINK),
    M("Slack answer blocks are not budgeted", SLK,
      ("ANSWER_BLOCK_BUDGET = MAX_BLOCKS - 3", "ANSWER_BLOCK_BUDGET = MAX_BLOCKS + 200"), tests=T_SINK),
    M("Slack Retry-After is not capped", SLK,
      ("    return min(max(v, 1.0), MAX_RETRY_AFTER_S)", "    return max(v, 1.0)"), tests=T_SINK),
    M("Slack 403 is dead instead of a pause", SLK,
      ("        if status == 403:\n            return Outcome.pause(reason, status=status)\n", ""), tests=T_SINK),
    M("Make queue full is dead instead of a retry", MKE,
      ('        if status == 400 and res.text.strip() == "Queue is full":', "        if False:"), tests=T_SINK),
    M("Make 410 is dead instead of a pause", MKE,
      ("        if status == 410:\n            return Outcome.pause(\"hook inactive (410)\", status=status)\n", ""), tests=T_SINK),
    M("Make api key header not sent", MKE, ("        if ctx.apikey:\n            headers[\"x-make-apikey\"] = ctx.apikey", "        if False:\n            headers[\"x-make-apikey\"] = ctx.apikey"),
      tests=T_SINK),
    M("Make idempotency header not sent", MKE, ('        headers = {"Content-Type": "application/json", "Idempotency-Key": event.event_id}', '        headers = {"Content-Type": "application/json"}'),
      tests=T_SINK),
    M("Make payload size cap removed", MKE, ("        if len(body) > MAX_BODY_BYTES:", "        if False:"), tests=T_SINK),
    M("Make requires the body Accepted", MKE, ("        if 200 <= status < 300:\n            return Outcome.done(status=status, effect_ref=\"queue\")",
                                              "        if status == 200 and res.text.strip() == \"Accepted\":\n            return Outcome.done(status=status, effect_ref=\"queue\")"), tests=T_SINK),
    M("a connect timeout is treated as an ambiguous timeout", HTP,
      ("        except (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError) as exc:", "        except (httpx.PoolTimeout, httpx.ConnectError) as exc:"),
      ("        except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:", "        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout) as exc:"),
      tests=["tests/test_http.py"]),
    # ---- the fakes themselves (a fake that accepts everything is a rubber stamp) -------------------------------------------------
    M("fake Slack: escaping rule deleted", FSLACK, ('        self.rules = {"escape", "verbatim", "limits"} if strict else {"limits"}', '        self.rules = {"verbatim", "limits"} if strict else {"limits"}'), tests=T_SINK),
    M("fake Slack: verbatim rule deleted", FSLACK, ('        self.rules = {"escape", "verbatim", "limits"} if strict else {"limits"}', '        self.rules = {"escape", "limits"} if strict else {"limits"}'), tests=T_SINK),
    M("fake Slack: length limits deleted", FSLACK, ('        self.rules = {"escape", "verbatim", "limits"} if strict else {"limits"}', '        self.rules = {"escape", "verbatim"} if strict else set()'), tests=T_SINK),
    # ---- configuration guards ------------------------------------------------------------------------------------------------------
    M("RELAY_LIVE guard removed", CFG, ("    if not live:\n        raise ConfigError(f\"{what} points at a non-local host and RELAY_LIVE is not 1\")", "    if False:\n        raise ConfigError(f\"{what} points at a non-local host and RELAY_LIVE is not 1\")"), tests=T_CFG),
    M("https no longer required when live", CFG, ('    if parsed.scheme != "https":', "    if False:"), tests=T_CFG),
    M("SQLite allowed in prod without the single container conditions", CFG, ("        if not (db_path and os.path.isabs(db_path) and inline_worker):", "        if False:"), tests=T_CFG),
    M("a separate once worker may use SQLite in prod", CFG, ('        if role == "worker_once":', "        if False:"), tests=T_CFG + ["tests/test_worker.py"]),
    M("an unset RELAY_ENV means dev", CFG, ('relay_env = env.get("RELAY_ENV") or "prod"', 'relay_env = env.get("RELAY_ENV") or "dev"'), tests=T_CFG),
    M("test signature key honoured outside RELAY_ENV=test", CFG, ('    if override and relay_env == "test":', "    if override:"), tests=T_CFG),
    M("a literal secret key in the JSON is accepted", CFG, ("        if k in LITERAL_SECRET_KEYS:", "        if False:"), tests=T_CFG),
    M("unknown config keys are accepted", CFG, ("        if k not in allowed:", "        if False:"), tests=T_CFG),
    M("the admin token may equal an ingest token", CFG, ("        if admin and admin in (ingest_token, previous):", "        if False:"), tests=T_CFG),
    M("short tokens are accepted", CFG, ("            if tok and len(tok) < MIN_TOKEN_CHARS:", "            if False:"), tests=T_CFG),
    M("the alert webhook skips the base URL guard", CFG, ('        check_base_url(alert_url, live, "the alert webhook")', "        pass"), tests=T_CFG),
    M("a sink URL skips the base URL guard", CFG, ('            check_base_url(url, live, f"{where} url")', "            pass"), tests=T_CFG),
    M("log redaction registry skipped when the config loads", CFG, ("    redact.register(*cfg.secret_values())\n    return cfg", "    return cfg"), tests=T_CFG),
    # ---- ingress ----------------------------------------------------------------------------------------------------------------------
    M("secret comparison is not constant time", APP, ('    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))', "    return a == b"), tests=T_APP),
    M("the previous ingest token is not accepted during rotation", APP, ("_any_match(presented, cfg.ingest_token, cfg.ingest_token_previous)", "_any_match(presented, cfg.ingest_token)"), tests=T_APP),
    M("the Idempotency-Key check removed", APP, ("        if not key or len(key) > MAX_KEY_CHARS or not key.isprintable():", "        if False:"), tests=T_APP),
    M("webhookId mismatch accepted", APP, ("        if webhook_id is not None and webhook_id != key:", "        if False:"), tests=T_APP),
    M("per route body cap not applied", APP, ("        request.max_content_length = cap\n", ""), tests=T_APP),
    M("an unmapped location is acknowledged", APP,
      ('            alert_routing(cfg.tenant_for_location(payload.get("locationId")), str(exc))\n            return _err(503, "unrouted")', '            alert_routing(cfg.tenant_for_location(payload.get("locationId")), str(exc))\n            return jsonify({"ok": True})'), tests=T_APP),
    M("a store failure on /ingest is acknowledged", APP,
      ('            log.error("ingest failed before commit: %s", type(exc).__name__)\n            return _err(503, "store")', '            log.error("ingest failed before commit: %s", type(exc).__name__)\n            return jsonify({"ok": True})'), tests=T_APP),
    M("a store failure on the workflow route is acknowledged", APP,
      ('            log.error("workflow ingest failed before commit: %s", type(exc).__name__)\n            return _err(503, "store")', '            log.error("workflow ingest failed before commit: %s", type(exc).__name__)\n            return jsonify({"ok": True})'), tests=T_APP),
    M("a workflow body without event_key is accepted", APP, ('        if event_key is None and tcfg.dedupe_mode != "body_hash":', "        if False:"), tests=T_APP),
    M("the workflow key is built from the contact id alone", APP,
      ("            key, basis = ghl.derive_key(tenant, payload, raw, event_key=event_key)", "            key, basis = ghl.sha256_hex(tenant, str(payload.get('contact_id'))), 'body-hash'"), tests=T_APP),
    M("the admin routes accept the ingest token", APP, ("_any_match(_bearer(request.headers.get(\"Authorization\")), cfg.admin_token)", "_any_match(_bearer(request.headers.get(\"Authorization\")), cfg.ingest_token)"), tests=T_APP),
    M("replay of a done row allowed without a sink", APP, ("        if force and not sink:", "        if False:"), tests=T_APP),
    M("the app does not install the log mask", APP, ("    redact.install()\n    redact.register(*cfg.secret_values())", "    redact.register(*cfg.secret_values())"), tests=T_APP + T_SEC),
    M("the app does not register the secrets for redaction", APP, ("    redact.register(*cfg.secret_values())\n    if deps is None:", "    if deps is None:"), tests=T_APP),
    # ---- GoHighLevel -------------------------------------------------------------------------------------------------------------------
    M("synthesised key built from the contact id only", GHL,
      ("        return sha256_hex(tenant, str(payload[\"type\"]), entity_id, stamp, sha256_hex(raw)), BASIS_SYNTHESISED", "        return sha256_hex(tenant, entity_id), BASIS_SYNTHESISED"), tests=T_GHL),
    M("body-hash key replaced by the contact id", GHL,
      ("    return sha256_hex(tenant, raw), BASIS_BODY_HASH", "    return sha256_hex(tenant, str(payload.get('id'))), BASIS_BODY_HASH"), tests=T_GHL + T_APP),
    M("appointment identity read from the flat shape", GHL,
      ("    if kind.startswith(\"Appointment\"):\n        return _appointment_identity(payload)", "    if kind.startswith(\"Appointment\"):\n        return _flat_identity(payload)"), tests=T_GHL),
    M("Ed25519 raw bytes path removed", GHL, ('    if _check(key, sig, raw):\n        return "raw"', "    if False:\n        return \"raw\""), tests=T_GHL),
    M("Ed25519 re-serialised path removed", GHL, ("    if reser is not None and reser != raw and _check(key, sig, reser):", "    if False:"), tests=T_GHL),
    M("the legacy X-WH-Signature header is accepted", GHL, ('    return headers.get("X-GHL-Signature") or None', '    return headers.get("X-GHL-Signature") or headers.get("X-WH-Signature")'), tests=T_GHL),
    M("the timestamp window ignores a future timestamp", GHL, ("    return not (now_ms - sent_ms > MAX_AGE_MS or sent_ms - now_ms > MAX_FUTURE_MS)", "    return not (now_ms - sent_ms > MAX_AGE_MS)"), tests=T_GHL),
    M("a flat attribution key wins over the nested object", GHL,
      ("    sources.append(payload)\n    for field, keys in _ATTR_FLAT.items():", "    sources.insert(0, payload)\n    for field, keys in _ATTR_FLAT.items():"), tests=T_GHL),
    M("normalise_lead raises on a payload that is not an object", GHL, ("    if not isinstance(payload, dict):\n        return _empty_lead()\n    inner", "    inner"), tests=T_GHL),
    # ---- redaction -----------------------------------------------------------------------------------------------------------------------
    M("registered secrets are not masked by value", RED, ("    for s in secrets:\n        text = text.replace(s, MASK)\n", ""), tests=T_SEC),
    M("secrets are masked shortest first", RED, ("secrets = sorted(_secrets, key=len, reverse=True)", "secrets = sorted(_secrets, key=len)"), tests=T_SEC),
    M("query values named api_key, access_token and token are not masked", RED, ("    text = _QUERY.sub(lambda m: m.group(1) + MASK, text)\n", ""), tests=T_SEC),
    M("Bearer values are not masked", RED, ("    return _BEARER.sub(lambda m: m.group(1) + MASK, text)", "    return text"), tests=T_SEC),
    M("exception text is not masked", RED, ("            record.exc_text = mask(logging.Formatter().formatException(record.exc_info))", "            pass"), tests=T_SEC),
    M("the httpx and httpcore loggers stay chatty", RED,
      ('    logging.getLogger("httpx").setLevel(logging.WARNING)\n    logging.getLogger("httpcore").setLevel(logging.WARNING)\n', ""), tests=T_SEC),
    M("a short value is registered as a secret", RED, ("_MIN_SECRET_LEN = 4", "_MIN_SECRET_LEN = 0"), tests=T_SEC),
]


def apply(mutant: Mutant, root: Path) -> str | None:
    """Edits the copy. Returns an error text when a pattern is not found exactly once."""
    path = root / "relay" / mutant.file
    text = path.read_text(encoding="utf-8")
    for old, new in mutant.edits:
        n = text.count(old)
        if n != 1:
            return f"pattern found {n} times: {old[:70]!r}"
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    return None


def copy_tree(dest: Path) -> Path:
    shutil.copytree(RELAY, dest / "relay", ignore=IGNORE)
    for rel in EXTRA_FROM_REPO:
        src = REPO / rel
        if src.exists():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
    return dest


def pytest(root: Path, tests: list[str], stores: str, extra: list[str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "RELAY_TEST_STORES": stores, "PYTHONDONTWRITEBYTECODE": "1"}
    cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *(extra or []), *(tests or ["tests"])]
    return subprocess.run(
        cmd, cwd=root / "relay", env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT_S
    )


def first_failure(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("FAILED") or line.startswith("ERROR"):
            return line[:150]
    return "non-zero exit"


def run_one(mutant: Mutant) -> tuple[str, str]:
    """('caught' | 'SURVIVED' | 'BROKEN' | 'NOT RUN', detail)"""
    if "postgres" in mutant.stores and not os.environ.get("DATABASE_URL_TEST"):
        return "NOT RUN", "needs DATABASE_URL_TEST"
    with tempfile.TemporaryDirectory(prefix="relay-mutant-") as d:
        root = copy_tree(Path(d))
        problem = apply(mutant, root)
        if problem:
            return "BROKEN", problem
        compiled = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", str(root / "relay")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if compiled.returncode != 0:
            return "BROKEN", "the mutated tree does not compile"
        try:
            targeted = pytest(root, mutant.tests, mutant.stores, [f"--deselect={t}" for t in SLOW_TESTS])
            if targeted.returncode != 0:
                if COLLECTION_ERROR.search(targeted.stdout):
                    return "BROKEN", "the mutated tree does not collect (an import error is not a catch)"
                return "caught", first_failure(targeted.stdout)
            full = pytest(root, [], mutant.stores)
            if full.returncode != 0:
                return "caught", "only by the full suite: " + first_failure(full.stdout)
        except subprocess.TimeoutExpired:
            return "caught", f"timed out after {RUN_TIMEOUT_S}s (a hang counts as a failure, but investigate)"
        return "SURVIVED", "the whole suite stayed green"


def baseline(groups: list[tuple[tuple[str, ...], str]]) -> list[str]:
    """The unmutated tree must pass every test selection a mutant will use, or 'caught' means nothing."""
    bad: list[str] = []
    with tempfile.TemporaryDirectory(prefix="relay-baseline-") as d:
        root = copy_tree(Path(d))
        for tests, stores in groups:
            res = pytest(root, list(tests), stores)
            if res.returncode != 0:
                bad.append(f"{' '.join(tests) or 'tests'} [{stores}]: {first_failure(res.stdout)}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check", action="store_true", help="only verify every pattern occurs exactly once")
    ap.add_argument("--only", default="")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--no-baseline", action="store_true", help="skip the unmutated runs (CI has just run the whole suite)")
    args = ap.parse_args()

    selected = [m for m in MUTANTS if args.only in m.name or args.only in m.file]
    if args.list:
        for m in selected:
            print(m.name)
        return 0
    names = [m.name for m in MUTANTS]
    if len(set(names)) != len(names):
        print("duplicate mutant names")
        return 1
    if args.check:
        bad = 0
        for m in selected:
            text = (RELAY / m.file).read_text(encoding="utf-8")
            for old, _new in m.edits:
                n = text.count(old)
                if n != 1:
                    print(f"BROKEN MUTANT  {m.name}: pattern found {n} times: {old[:70]!r}")
                    bad += 1
                    break
        print(f"{len(selected)} mutants checked, {bad} broken")
        return 1 if bad else 0

    if not args.no_baseline:
        groups = sorted({(tuple(m.tests), m.stores) for m in selected if not ("postgres" in m.stores and not os.environ.get("DATABASE_URL_TEST"))})
        failures = baseline([(t, s) for t, s in groups] + [((), "sqlite")])
        if failures:
            print("BASELINE IS NOT GREEN, mutants would prove nothing:")
            print("\n".join("  " + f for f in failures))
            return 1

    problems = 0
    not_run = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for mutant, (status, detail) in zip(selected, pool.map(run_one, selected)):
            print(f"{status:<9} {mutant.name}" + ("" if status == "caught" else f"  ({detail})"), flush=True)
            if status == "NOT RUN":
                not_run += 1
            elif status != "caught":
                problems += 1
    today = datetime.date.today().isoformat()
    if problems:
        print(f"\n{problems} PROBLEM(S) in {len(selected)} mutants, run {today}")
        return 1
    if not_run:
        print(f"\n{not_run} mutant(s) NOT RUN (set DATABASE_URL_TEST); {len(selected) - not_run} caught, run {today}")
        return 2
    print(f"\nALL {len(selected)} MUTANTS CAUGHT, run {today}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
