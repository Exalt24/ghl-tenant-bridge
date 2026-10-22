# LIFESPAN: permanent (the mutation check for relay/, run by CI on every push and by hand after any guard change)
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
BASE = "relay/sinks/base.py"
ATT = "relay/attribution.py"
CAP = "relay/sinks/capi.py"
SHT = "relay/sinks/sheets.py"
FMETA = "tests/fakes/meta_graph.py"
FSHEETS = "tests/fakes/sheets.py"

T_STORE = ["tests/test_store_contract.py"]
T_FAN = ["tests/test_fanout.py"]
T_APP = ["tests/test_app.py"]
T_CFG = ["tests/test_config.py"]
T_GHL = ["tests/test_ghl.py"]
T_SINK = ["tests/test_sinks.py"]
T_CRASH = ["tests/test_crash_and_concurrency.py"]
T_SEC = ["tests/test_secrets.py", "tests/test_redact.py"]
T_ATTR = ["tests/test_attribution.py"]
T_CAPI = ["tests/test_capi.py"]
T_SHEETS = ["tests/test_sheets.py"]
T_ROUTE = ["tests/test_ghl_route.py"]
T_STORE2 = ["tests/test_store_tier2.py"]
T_CONTRACTS = ["tests/test_contracts.py"]

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
      ('f"UPDATE deliveries SET claim_until = {self.now(lease_s)}, claim_token = ?, claims = claims + 1 "',
       "f\"UPDATE deliveries SET claim_until = '{__import__('datetime').datetime.fromtimestamp(__import__('time').time() + lease_s, __import__('datetime').timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}', claim_token = ?, claims = claims + 1 \""),
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
      ('            f"created_at = {self.now()}, "\n', ""),
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
      ("            total = sum(pool.map(lambda s: self._drain_sink_and_release(s, wait_horizon_s, max_total_wait_s), sinks))",
       "            total = 0\n            for s in sinks:\n                total += pool.submit(self._drain_sink_and_release, s, wait_horizon_s, max_total_wait_s).result()"),
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
      ('            return _err(503, "unrouted")\n        except Exception as exc:\n            log.error("ingest failed before commit: %s", type(exc).__name__)', '            return jsonify({"ok": True})\n        except Exception as exc:\n            log.error("ingest failed before commit: %s", type(exc).__name__)'), tests=T_APP),
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
    M("normalise_lead raises on a payload that is not an object", GHL, ("    if not isinstance(payload, dict):\n        return _empty_lead()\n    if isinstance(payload.get(\"data\"), dict):", "    if isinstance(payload.get(\"data\"), dict):"), tests=T_GHL),
    # ---- redaction -----------------------------------------------------------------------------------------------------------------------
    M("registered secrets are not masked by value", RED, ("    for s in secrets:\n        text = text.replace(s, MASK)\n", ""), tests=T_SEC),
    M("secrets are masked shortest first", RED, ("secrets = sorted(_secrets, key=len, reverse=True)", "secrets = sorted(_secrets, key=len)"), tests=T_SEC),
    M("query values named api_key, access_token and token are not masked", RED, ("    text = _QUERY.sub(lambda m: m.group(1) + MASK, text)\n", ""), tests=T_SEC),
    M("Bearer values are not masked", RED, ("    return _BEARER.sub(lambda m: m.group(1) + MASK, text)", "    return text"), tests=T_SEC),
    M("exception text is not masked", RED, ("            record.exc_text = mask(logging.Formatter().formatException(record.exc_info))", "            pass"), tests=T_SEC),
    M("the httpx and httpcore loggers stay chatty", RED,
      ('    logging.getLogger("httpx").setLevel(logging.WARNING)\n    logging.getLogger("httpcore").setLevel(logging.WARNING)\n', ""), tests=T_SEC),
    M("a short value is registered as a secret", RED, ("_MIN_SECRET_LEN = 4", "_MIN_SECRET_LEN = 0"), tests=T_SEC),
    # ---- tier 2: attribution and hashing ---------------------------------------------------------------------------------------------
    M("email hash skips lowercase", ATT, ('        return None\n    return s.lower()\n', '        return None\n    return s\n'), tests=T_ATTR),
    M("email is not trimmed before hashing", ATT, ('def norm_email(value: Any) -> str | None:\n    """Trim and lowercase. A value with no @ is not an email, so it is left out rather than hashed."""\n    s = _clean(value)',
                                                    'def norm_email(value: Any) -> str | None:\n    """Trim and lowercase. A value with no @ is not an email, so it is left out rather than hashed."""\n    s = value if isinstance(value, str) else None'), tests=T_ATTR),
    M("a value with no @ is hashed as an email", ATT, ('    if s is None or "@" not in s:\n        return None\n    return s.lower()', '    if s is None:\n        return None\n    return s.lower()'), tests=T_ATTR),
    M("phone leading zeros are not stripped", ATT, ('    digits = digits.lstrip("0")\n', '    pass\n'), tests=T_ATTR),
    M("the default country code is put in front of any number", ATT, ('    elif digits.startswith("0"):\n        if not default_country_code:', '    elif True:\n        if not default_country_code:'), tests=T_ATTR),
    M("a bare number with no country code marker is accepted", ATT, ('    elif len(digits) < 11:\n        return None\n', '    elif False:\n        return None\n'), tests=T_ATTR),
    M("the phone length check is removed", ATT, ('    if not 8 <= len(digits) <= 15:\n        return None\n    return digits', '    if False:\n        return None\n    return digits'), tests=T_ATTR),
    M("a plus prefix is no longer trusted as a country code", ATT, ('    if raw.startswith("+"):\n        pass\n', '    if False:\n        pass\n'), tests=T_ATTR),
    M("the 00 international prefix is read as a national zero", ATT, ('    elif digits.startswith("00"):\n        digits = digits[2:]', '    elif False:\n        digits = digits[2:]'), tests=T_ATTR),
    M("names are not lowercased", ATT, ('    out = " ".join(_drop_punctuation(s.lower()).split())', '    out = " ".join(_drop_punctuation(s).split())'), tests=T_ATTR),
    M("names keep their punctuation", ATT, ('    out = " ".join(_drop_punctuation(s.lower()).split())', '    out = " ".join(s.lower().split())'), tests=T_ATTR),
    M("a city keeps its spaces", ATT, ('    return s.replace(" ", "") if s else None', '    return s'), tests=T_ATTR),
    M("a state outside the US is cut to two letters", ATT, ('    out = _drop_punctuation(low).replace(" ", "")\n    return out or None', '    out = _drop_punctuation(low).replace(" ", "")[:2]\n    return out or None'), tests=T_ATTR),
    M("a US state name is not mapped to its code", ATT, ('        mapped = US_STATES.get(low.replace(".", ""))\n        if mapped:', '        mapped = None\n        if mapped:'), tests=T_ATTR),
    M("a US zip is not cut to five digits", ATT, ('        return s[:5]\n', '        return s\n'), tests=T_ATTR),
    M("a zip keeps its dash", ATT, ('    out = s.lower().replace(" ", "").replace("-", "")', '    out = s.lower().replace(" ", "")'), tests=T_ATTR),
    M("a country is not lowercased", ATT, ('    low = s.lower()\n    if re.fullmatch(r"[a-z]{2}", low):', '    low = s\n    if re.fullmatch(r"[a-z]{2}", low):'), tests=T_ATTR),
    M("client_ip_address is hashed", ATT, ('            out["client_ip_address"] = str(ipaddress.ip_address(ip))', '            out["client_ip_address"] = sha256_hex(str(ipaddress.ip_address(ip)))'), tests=T_ATTR + T_CAPI),
    M("client_ip_address is not validated", ATT, ('            out["client_ip_address"] = str(ipaddress.ip_address(ip))', '            out["client_ip_address"] = ip'), tests=T_ATTR),
    M("client_user_agent is hashed", ATT, ('        out["client_user_agent"] = ua[:MAX_USER_AGENT_CHARS]', '        out["client_user_agent"] = sha256_hex(ua[:MAX_USER_AGENT_CHARS])'), tests=T_ATTR + T_CAPI),
    M("fbc is hashed", ATT, ('        out["fbc"] = fbc\n', '        out["fbc"] = sha256_hex(fbc)\n'), tests=T_ATTR + T_CAPI),
    M("fbp is hashed", ATT, ('        out["fbp"] = fbp\n', '        out["fbp"] = sha256_hex(fbp)\n'), tests=T_ATTR + T_CAPI),
    M("fbc is built in seconds not milliseconds", ATT, ('    return round(when * 1000)', '    return round(when)'), tests=T_ATTR),
    M("the fbclid is lowercased in the fbc", ATT, ('    return f"fb.{FBC_SUBDOMAIN_INDEX}.{creation_ms}.{fbclid}"', '    return f"fb.{FBC_SUBDOMAIN_INDEX}.{creation_ms}.{fbclid.lower()}"'), tests=T_ATTR),
    M("the fbc subdomain index is 0", ATT, ("FBC_SUBDOMAIN_INDEX = 1", "FBC_SUBDOMAIN_INDEX = 0"), tests=T_ATTR),
    M("a given fbc is ignored", ATT, ("    if given:\n        return given\n", "    if False:\n        return given\n"), tests=T_ATTR),
    M("the fbc creation time ignores the contact date", ATT, ('    when = parse_time(lead.get("date_added"))', "    when = None"), tests=T_ATTR),
    M("the fbc creation time ignores the received time", ATT, ("        when = received_epoch if received_epoch is not None else fallback_epoch", "        when = fallback_epoch"), tests=T_ATTR),
    M("UTM values are not sanitised", ATT, ("    if not all(ch.isalnum() or ch in UTM_PUNCTUATION for ch in s):\n        return None\n", "    if False:\n        return None\n"), tests=T_ATTR + T_CAPI),
    M("UTM values are not limited to 200 characters", ATT, ("    return s[:UTM_MAX_CHARS]", "    return s"), tests=T_ATTR),
    M("the URL query is kept whole", ATT, ('    return urlunsplit((parts.scheme, netloc, parts.path, "&".join(kept), ""))', '    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))'), tests=T_ATTR + T_CAPI),
    M("the URL userinfo is kept", ATT, ('    netloc = parts.hostname + (f":{port}" if port else "")', "    netloc = parts.netloc"), tests=T_ATTR),
    M("the URL fragment is kept", ATT, ('"&".join(kept), ""))', '"&".join(kept), parts.fragment))'), tests=T_ATTR),
    M("a non http URL is accepted", ATT, ('    if parts.scheme not in ("http", "https") or not parts.hostname:', "    if not parts.hostname:"), tests=T_ATTR),
    M("UTM values inside a URL are not sanitised", ATT, ("            clean = sanitise_utm(val)", "            clean = val"), tests=T_ATTR),
    M("every URL parameter that looks like an id is kept", ATT, ("        elif low in CLICK_ID_PARAMS:", "        elif True:"), tests=T_ATTR),
    M("an allowlisted source no longer counts as a click id", ATT, ('    return bool(source and allowed_sources and source.lower() in {a.lower() for a in allowed_sources})', "    return False"), tests=T_ATTR + T_CAPI),
    M("fbp no longer counts as a click id", ATT, ('    if any(attr.get(k) for k in ("fbc", "fbp", "fbclid")):', '    if any(attr.get(k) for k in ("fbc", "fbclid")):'), tests=T_ATTR),
    M("the never-hash list loses fbp", ATT, ('NEVER_HASH = ("client_ip_address", "client_user_agent", "fbc", "fbp")', 'NEVER_HASH = ("client_ip_address", "client_user_agent", "fbc")'), tests=T_ATTR),
    # ---- tier 2: the Meta Conversions API sink -----------------------------------------------------------------------------------------
    M("CAPI event_id dropped", CAP, ('            "event_id": make_event_id(event, name),\n', ""), tests=T_CAPI),
    M("CAPI event_time is not checked against the 7 day rule", CAP, ("        if now - when > MAX_EVENT_AGE_S:", "        if False:"), tests=T_CAPI),
    M("CAPI 7 day rule has no hour of margin", CAP, ("MAX_EVENT_AGE_S = SEVEN_DAYS_S - MARGIN_S", "MAX_EVENT_AGE_S = SEVEN_DAYS_S"), tests=T_CAPI),
    M("CAPI event_time in the future is not clamped", CAP, ("        when = min(when, now)", "        when = when"), tests=T_CAPI),
    M("CAPI test_event_code is never sent", CAP, ('        if not ctx.extra["send_live"]:\n            body["test_event_code"] = ctx.extra["test_event_code"]', "        if False:\n            body[\"test_event_code\"] = ctx.extra[\"test_event_code\"]"), tests=T_CAPI),
    M("CAPI test_event_code is sent even when live", CAP, ('        if not ctx.extra["send_live"]:\n            body["test_event_code"]', '        if True:\n            body["test_event_code"]'), tests=T_CAPI),
    M("CAPI live is allowed without RELAY_LIVE", CAP, ('        if wants_live and not live:\n            raise ValueError("live is true but RELAY_LIVE is not 1")', "        if False:\n            raise ValueError(\"live is true but RELAY_LIVE is not 1\")"), tests=T_CAPI),
    M("CAPI test mode needs no code", CAP, ("        if not wants_live and not code:", "        if False:"), tests=T_CAPI),
    M("CAPI action_source is always website", CAP, ('"action_source": "website" if ("client_user_agent" in user_data and url) else "system_generated",', '"action_source": "website",'), tests=T_CAPI),
    M("CAPI website needs only a user agent", CAP, ('if ("client_user_agent" in user_data and url) else', 'if "client_user_agent" in user_data else'), tests=T_CAPI),
    M("CAPI website needs only a url", CAP, ('if ("client_user_agent" in user_data and url) else', "if url else"), tests=T_CAPI),
    M("CAPI event_source_url is sent unsanitised", CAP, ('        url = attribution.sanitise_url(attr.get("page_url"))', '        url = attr.get("page_url")'), tests=T_CAPI),
    M("CAPI event id is not a hash of tenant, entity and name", CAP, ('    return attribution.sha256_hex(f"{event.tenant}|{part}|{name}")', "    return event.event_id"), tests=T_CAPI),
    M("CAPI page event id is ignored", CAP, ("    if isinstance(given, str) and given:\n        return given\n", "    if False:\n        return given\n"), tests=T_CAPI),
    M("CAPI purchase event id is built from the contact", CAP, ('        value = payload.get("id")\n    elif kind.startswith("Appointment"):', '        value = event.lead.get("contact_id")\n    elif kind.startswith("Appointment"):'), tests=T_CAPI),
    M("CAPI unmapped event type is not skipped", CAP, ("        if not name:\n            return None, Outcome.skipped(", "        if False:\n            return None, Outcome.skipped("), tests=T_CAPI),
    M("CAPI opportunity that is not won is sent", CAP, ('            if kind.startswith("Opportunity") and (lead.get("status") or "").lower() != "won":', "            if False:"), tests=T_CAPI),
    M("CAPI won status is matched case sensitively", CAP, ('(lead.get("status") or "").lower() != "won"', '(lead.get("status") or "") != "won"'), tests=T_CAPI),
    M("CAPI purchase without a currency is sent", CAP, ('            if not extra.get("currency"):', "            if False:"), tests=T_CAPI),
    M("CAPI purchase without a value is sent", CAP, ('            if lead.get("monetary_value") is None:', "            if False:"), tests=T_CAPI),
    M("CAPI lead with no click id is sent", CAP, ('        if extra["require_click_id"] and not attribution.has_click_id(lead, extra["click_id_sources"]):', "        if False:"), tests=T_CAPI),
    M("CAPI default requires no click id", CAP, ('        require = options.get("capi_require_click_id", True)', '        require = options.get("capi_require_click_id", False)'), tests=T_CAPI),
    M("CAPI lead with nothing to match on is sent", CAP, ("        if not any(k in user_data for k in MATCH_KEYS):", "        if False:"), tests=T_CAPI),
    M("CAPI create event uses dateUpdated", CAP, ('        candidates = [lead.get("date_added")]', '        candidates = [lead.get("date_updated")]'), tests=T_CAPI),
    M("CAPI status change uses dateAdded", CAP, ('        candidates = [lead.get("date_updated"), event.payload.get("timestamp")]', '        candidates = [lead.get("date_added")]'), tests=T_CAPI),
    M("CAPI status change ignores the envelope timestamp", CAP, ('        candidates = [lead.get("date_updated"), event.payload.get("timestamp")]', '        candidates = [lead.get("date_updated")]'), tests=T_CAPI),
    M("CAPI event_time fallback ignores the received time", CAP, ("    return event.received_epoch if event.received_epoch is not None else now", "    return now"), tests=T_CAPI),
    M("CAPI success needs exactly a 200", CAP, ("        if 200 <= status < 300:\n            return Outcome.done(status=status)", "        if status == 200:\n            return Outcome.done(status=status)"), tests=T_CAPI),
    M("CAPI throttle ignores the usage header", CAP, ("            return Outcome.retry(after_s=regain_wait_s(res.headers), status=status, reason=label, rate_limited=True)", "            return Outcome.retry(after_s=None, status=status, reason=label, rate_limited=True)"), tests=T_CAPI),
    M("CAPI regain time is read as seconds", CAP, ("waits.append(float(minutes) * 60.0)", "waits.append(float(minutes))"), tests=T_CAPI),
    M("CAPI regain time is not capped", CAP, ("            return min(max(waits), MAX_THROTTLE_WAIT_S)", "            return max(waits)"), tests=T_CAPI),
    M("CAPI usage header name is read case sensitively", CAP, ('BUC_HEADERS = ("x-business-use-case-usage", "x-business-use-case")', 'BUC_HEADERS = ("X-Business-Use-Case-Usage", "X-Business-Use-Case")'), tests=T_CAPI),
    M("CAPI the second usage header name is not read", CAP, ('BUC_HEADERS = ("x-business-use-case-usage", "x-business-use-case")', 'BUC_HEADERS = ("x-business-use-case-usage",)'), tests=T_CAPI),
    M("CAPI the 80000 series is not retried", CAP, ("    return 80000 <= code < 81000", "    return False"), tests=T_CAPI),
    M("CAPI a rejected token is dead not a pause", CAP, ('        if code in PAUSE_CODES:\n            return Outcome.pause(f"token rejected by Meta ({label})", status=status)', "        if False:\n            return Outcome.pause(f\"token rejected by Meta ({label})\", status=status)"), tests=T_CAPI),
    M("CAPI code 102 is not a pause", CAP, ("PAUSE_CODES = frozenset({190, 102})", "PAUSE_CODES = frozenset({190})"), tests=T_CAPI),
    M("CAPI temporary codes 1 and 2 are dead", CAP, ("RETRY_CODES = frozenset({1, 2, 4, 17, 341, 613})", "RETRY_CODES = frozenset({4, 17, 341, 613})"), tests=T_CAPI),
    M("CAPI throttle codes 341 is not a throttle", CAP, ("THROTTLE_CODES = frozenset({4, 17, 341, 613})", "THROTTLE_CODES = frozenset({4, 17, 613})"), tests=T_CAPI),
    M("CAPI 5xx is ambiguous instead of a retry", CAP, ("        if status >= 500:\n            return Outcome.retry(status=status, reason=label, breaker_failure=True)", "        if status >= 500:\n            return Outcome.ambiguous(status=status, reason=label)"), tests=T_CAPI),
    M("CAPI timeout is ambiguous instead of a retry", CAP, ("            # Safe to repeat: the same event_id and event_name is kept once, so this is a RETRY, not AMBIGUOUS.\n            return Outcome.retry(reason=exc.kind, breaker_failure=True)", "            return Outcome.ambiguous(reason=exc.kind)"), tests=T_CAPI),
    M("CAPI other client errors are retried for ever", CAP, ("        return Outcome.dead(label, status=status)", "        return Outcome.retry(status=status, reason=label)"), tests=T_CAPI),
    M("CAPI access token is not sent", CAP, ("/events?access_token={quote(ctx.apikey or '', safe='')}", "/events?access_token=none"), tests=T_CAPI),
    M("CAPI configured timeout is ignored", CAP, ("body=body, timeout=ctx.timeout_s)", "body=body, timeout=60.0)"), tests=T_CAPI),
    M("CAPI pixel id is not in the path", CAP, ("{quote(ctx.extra['pixel_id'], safe='')}/events", "none/events"), tests=T_CAPI),
    # ---- tier 2: configuration of the new sinks and routes --------------------------------------------------------------------------------
    M("an access token is not required for a sink that needs one", CFG, ('            if cls.needs_apikey and "apikey_env" not in s:', "            if False:"), tests=T_CFG + T_CAPI),
    M("a sink env map may name anything", CFG, ("            if not isinstance(env_map, dict) or any(k not in cls.required_env for k in env_map):", "            if not isinstance(env_map, dict):"), tests=T_CAPI),
    M("a missing required env name is not refused", CFG, ("                if need not in env_map:\n                    raise ConfigError(", "                if False:\n                    raise ConfigError("), tests=T_CAPI),
    M("a literal identifier in sink options is accepted", CFG, ("                if k in LITERAL_OPTION_KEYS:", "                if False:"), tests=T_CAPI + T_SHEETS),
    M("sink options of the wrong type are not refused", CFG, ('            if not isinstance(options, dict):\n                raise ConfigError(f"{where}: options must be an object")', '            if False:\n                raise ConfigError(f"{where}: options must be an object")'), tests=T_CAPI),
    M("the sink secrets are not registered for redaction", CFG, ("                secrets=cls.secret_values(env_values),", "                secrets=[],"), tests=T_CAPI + T_SHEETS),
    M("the direct webhook size cap is not read from the config", CFG, ('    ghl_cap = int(_positive(raw, "ghl_webhook_max_body_bytes", DEFAULT_MAX_BODY_BYTES, "config"))', "    ghl_cap = DEFAULT_MAX_BODY_BYTES"), tests=T_ROUTE),
    # ---- tier 2: the direct HighLevel route --------------------------------------------------------------------------------------------
    M("the direct route accepts an unverified signature", APP, ('        if how is None:\n            return _err(401, "unauthorised")', "        if False:\n            return _err(401, \"unauthorised\")"), tests=T_ROUTE),
    M("the direct route does not tell a legacy only request apart", APP, ('"legacy-signature-refused" if request.headers.get("X-WH-Signature") else "unauthorised"', '"unauthorised"'), tests=T_ROUTE),
    M("the direct route ignores a native webhook id", APP, ("            key, basis = ghl.derive_key(tenant, payload, raw, native_id=native)", "            key, basis = ghl.derive_key(tenant, payload, raw, native_id=None)"), tests=T_ROUTE),
    M("the direct route trusts an oversized webhook id", APP, ("native = webhook_id if isinstance(webhook_id, str) and 0 < len(webhook_id) <= MAX_KEY_CHARS else None", "native = webhook_id if isinstance(webhook_id, str) and webhook_id else None"), tests=T_ROUTE),
    M("the direct route acknowledges a store failure", APP, ('            log.error("direct webhook ingest failed before commit: %s", type(exc).__name__)\n            return _err(503, "store")', '            log.error("direct webhook ingest failed before commit: %s", type(exc).__name__)\n            return jsonify({"ok": True})'), tests=T_ROUTE),
    M("the direct route acknowledges an unmapped location", APP, ('            alert_routing(cfg.tenant_for_location(payload.get("locationId")), str(exc))\n            return _err(503, "unrouted")\n        if cfg.tenants[tenant]', '            alert_routing(cfg.tenant_for_location(payload.get("locationId")), str(exc))\n            return jsonify({"ok": True})\n        if cfg.tenants[tenant]'), tests=T_ROUTE),
    M("the direct route ignores a tenant's timestamp window", APP, ("        if cfg.tenants[tenant].ghl_timestamp_window and not", "        if False and not"), tests=T_ROUTE),
    M("the direct route does not apply its own size cap", APP, ("        raw = read_body(cfg.ghl_webhook_max_body_bytes)", "        raw = read_body(10**9)"), tests=T_ROUTE),
    M("the envelope shape is not unwrapped", GHL, ('    if isinstance(payload.get("data"), dict):', "    if False:"), tests=T_GHL + T_CONTRACTS),
    M("a field inside the envelope beats the envelope's own key", GHL, ('        payload = {**payload["data"], **{k: v for k, v in payload.items() if k != "data"}}', '        payload = {**{k: v for k, v in payload.items() if k != "data"}, **payload["data"]}'), tests=T_GHL + T_CONTRACTS),
    # ---- tier 2: store queries ------------------------------------------------------------------------------------------------------------
    M("a claim does not count itself", STORE, ('claim_token = ?, claims = claims + 1 "', 'claim_token = ? "'), tests=T_STORE2),
    M("a lost claim is never noticed", STORE, ('prior_claim_lost=int(row["claims"]) - 1 > int(row["attempts"]) + int(row["retries"]),', "prior_claim_lost=False,"), tests=T_STORE2 + T_SHEETS),
    M("the claim counter is never brought back in line", STORE, ('                f"claims = attempts + ? + retries + ?, "\n', ""),
      ("                    # A finished try brings `claims` back in line with the tries counted, so a lost claim is remembered\n                    # by the maybe_acted mark below (set when this claim found one) and not by a permanent offset.\n                    plan.attempts_inc,\n                    plan.retries_inc,\n", ""),
      tests=T_STORE2),
    M("a lost claim leaves no maybe_acted mark", STORE, ("1 if (plan.maybe_acted or claim.prior_claim_lost) else 0,", "1 if plan.maybe_acted else 0,"), tests=T_STORE2),
    M("the maybe_acted mark is never stored", STORE, ("1 if (plan.maybe_acted or claim.prior_claim_lost) else 0,", "0,"), tests=T_STORE2),
    M("the age of the maybe_acted mark is not read", STORE, ('since_maybe_acted_s=None if row["since_maybe_acted_s"] is None else float(row["since_maybe_acted_s"]),', "since_maybe_acted_s=None,"), tests=T_STORE2),
    M("maybe_acted_before is never set", STORE, ('maybe_acted_before=row["maybe_acted_at"] is not None,', "maybe_acted_before=False,"), tests=T_STORE2),
    M("replay keeps the lost claim and the maybe_acted mark", STORE, ("claims = 0, maybe_acted_at = NULL, ", ""), tests=T_STORE2),
    M("get_event does not report the received time", STORE, ('            row["received_epoch"] = to_epoch(row["received_at"])', '            row["received_epoch"] = None'), tests=T_STORE2),
    M("the append lock never blocks", STORE, ("            return cur.rowcount == 1\n\n    def _lock_release", "            return True\n\n    def _lock_release"), tests=T_STORE2 + T_SHEETS),
    M("the append lock is never released", STORE, ('            t.execute("DELETE FROM append_locks WHERE lock_key = ? AND holder = ?", (key, holder))', "            pass"), tests=T_STORE2),
    M("an expired append lock is never reclaimed", STORE, ('f"WHERE append_locks.until <= {self.now()}",', 'f"WHERE 1 = 0",'), tests=T_STORE2),
    M("a caller that did not get the lock releases the holder's lock", STORE,
      ("            if got:\n                self._lock_release(key, holder)", "            self._lock_release(key, holder)"),
      ('            t.execute("DELETE FROM append_locks WHERE lock_key = ? AND holder = ?", (key, holder))', '            t.execute("DELETE FROM append_locks WHERE lock_key = ?", (key,))'),
      tests=T_STORE2),
    M("Postgres append lock is not taken", PG, ('        row = self._conn().execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS got", (key,)).fetchone()\n        return bool(row["got"])', "        return True"), tests=T_STORE2 + T_SHEETS, stores="postgres"),
    M("Postgres append lock is never released", PG, ('        self._conn().execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))', "        pass"), tests=T_STORE2, stores="postgres"),
    M("an ambiguous outcome carries no maybe_acted mark", BASE, ("breaker_failure=True, maybe_acted=True)", "breaker_failure=True)"), tests=T_SHEETS + T_FAN),
    M("the final write drops the maybe_acted mark", FAN, ("note=reason, min_interval_s=min_interval, maybe_acted=outcome.maybe_acted,", "note=reason, min_interval_s=min_interval,"), tests=T_SHEETS),
    M("a reconciled absent read is not marked as maybe acted", FAN, ("        absent.maybe_acted = True\n        return absent", "        return absent"), tests=T_SHEETS),
    # ---- tier 2: the Google Sheets sink ------------------------------------------------------------------------------------------------------
    M("Sheets values are sent USER_ENTERED instead of RAW", SHT, (':append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"', ':append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"'), tests=T_SHEETS),
    M("Sheets INSERT_ROWS is dropped", SHT, ("&insertDataOption=INSERT_ROWS", ""), tests=T_SHEETS),
    M("Sheets JWT lifetime is over 65 minutes", SHT, ("JWT_LIFETIME_S = 3600", "JWT_LIFETIME_S = 4200"), tests=T_SHEETS),
    M("Sheets JWT aud is not the token endpoint", SHT, ('"aud": account.token_uri,', '"aud": DEFAULT_TOKEN_URI,'), tests=T_SHEETS),
    M("Sheets JWT scope is wrong", SHT, ('"iss": account.email, "scope": SCOPE,', '"iss": account.email, "scope": "https://www.googleapis.com/auth/drive",'), tests=T_SHEETS),
    M("Sheets JWT is base64url with padding", SHT, ('    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")', '    return base64.urlsafe_b64encode(data).decode("ascii")'), tests=T_SHEETS),
    M("Sheets JWT is signed with SHA-1", SHT, ("padding.PKCS1v15(), hashes.SHA256())", "padding.PKCS1v15(), hashes.SHA1())"), tests=T_SHEETS),
    M("Sheets grant type literal is wrong", SHT, ('GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"', 'GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt_bearer"'), tests=T_SHEETS),
    M("Sheets token is cached for ever", SHT, ("            if cached is not None and not force and cached[1] - TOKEN_REFRESH_MARGIN_S > now:", "            if cached is not None and not force:"), tests=T_SHEETS),
    M("Sheets token is refreshed only when it has expired", SHT, ("TOKEN_REFRESH_MARGIN_S = 300", "TOKEN_REFRESH_MARGIN_S = 0"), tests=T_SHEETS),
    M("Sheets token is never cached", SHT, ("            if cached is not None and not force and cached[1] - TOKEN_REFRESH_MARGIN_S > now:", "            if False:"), tests=T_SHEETS),
    M("Sheets 401 does not refresh the token", SHT, ("        for refreshed in (False, True):", "        for refreshed in (False,):"), tests=T_SHEETS),
    M("Sheets bearer token is not sent", SHT, ('            headers = {"Authorization": f"Bearer {token}"}', "            headers = {}"), tests=T_SHEETS),
    M("Sheets token refusal is dead instead of a pause", SHT, ('        return Outcome.pause(f"token endpoint refused: http {res.status}" + (f" {code}" if code else ""), status=res.status)', '        return Outcome.dead(f"token endpoint refused: http {res.status}" + (f" {code}" if code else ""), status=res.status)'), tests=T_SHEETS),
    M("Sheets token endpoint 5xx is a pause", SHT, ("        if res.status == 429 or res.status >= 500:", "        if res.status == 429:"), tests=T_SHEETS),
    M("Sheets 429 backoff has no cap", SHT, ("    return min(2 ** max(retries, 0) + rng.random(), MAX_429_WAIT_S)", "    return 2 ** max(retries, 0) + rng.random()"), tests=T_SHEETS),
    M("Sheets 429 backoff has no jitter", SHT, ("    return min(2 ** max(retries, 0) + rng.random(), MAX_429_WAIT_S)", "    return min(2 ** max(retries, 0), MAX_429_WAIT_S)"), tests=T_SHEETS),
    M("Sheets 429 is not marked rate limited", SHT, ('Outcome.retry(after_s=backoff_429(ctx.retries, self.rng), status=status, reason="rate limited", rate_limited=True)', 'Outcome.retry(after_s=backoff_429(ctx.retries, self.rng), status=status, reason="rate limited")'), tests=T_SHEETS),
    M("Sheets 5xx is dead instead of ambiguous", SHT, ('        if status >= 500:\n            return Outcome.ambiguous(status=status, reason=f"http {status}")', '        if status >= 500:\n            return Outcome.dead(f"http {status}", status=status)'), tests=T_SHEETS),
    M("Sheets timeout is dead instead of ambiguous", SHT, ("        except (HttpTimeout, HttpFailure) as exc:\n            return Outcome.ambiguous(reason=exc.kind)", "        except (HttpTimeout, HttpFailure) as exc:\n            return Outcome.dead(exc.kind)"), tests=T_SHEETS),
    M("Sheets 403 is not a pause", SHT, ("        if status == 403:  # ASSUMED meaning: the sheet is not shared with the service account (a tenant level fault)", "        if False:  # ASSUMED meaning: the sheet is not shared with the service account (a tenant level fault)"), tests=T_SHEETS),
    M("Sheets 404 is a pause instead of dead", SHT, ('            return Outcome.dead("spreadsheet not found (404)", status=status)', '            return Outcome.pause("spreadsheet not found (404)", status=status)'), tests=T_SHEETS),
    M("Sheets table start is not checked", SHT, ('        if start is not None and start != "A":', "        if False:"), tests=T_SHEETS),
    M("Sheets accepts a table that starts at column B", SHT, ('        if start is not None and start != "A":', '        if start is not None and start not in ("A", "B"):'), tests=T_SHEETS),
    M("Sheets does not remember a refused table", SHT, ('            with self._mutex:\n                self._bad_start[(ctx.extra["spreadsheet_id"], ctx.extra["sheet"])] = self.clock()', "            pass"), tests=T_SHEETS),
    M("Sheets keeps refusing a table for ever", SHT, ("        if refused_at is not None and now - refused_at < 900.0:", "        if refused_at is not None:"), tests=T_SHEETS),
    M("Sheets reconcile before send is skipped", SHT, ("        if may_have_written:\n            early = self._lookup_before_send(event, http, ctx)", "        if False:\n            early = self._lookup_before_send(event, http, ctx)"), tests=T_SHEETS),
    M("Sheets does not look again inside the lock", SHT, ("            if may_have_written:\n                # Looked again right before the append", "            if False:\n                # Looked again right before the append"), tests=T_SHEETS),
    M("Sheets an absent read counts at once", SHT, ("        if settled or self._quiet_long_enough(ctx):\n            return None", "        if True:\n            return None"), tests=T_SHEETS),
    M("Sheets the absent wait is thirty seconds", SHT, ("ABSENT_AFTER_S = 60.0", "ABSENT_AFTER_S = 30.0"), tests=T_SHEETS),
    M("Sheets appends when the read-back failed", SHT, ('        if ids is None:\n            return Outcome.retry(reason="could not read the sheet back to check for a late append", breaker_failure=True)', "        if False:\n            return Outcome.retry(reason=\"could not read the sheet back to check for a late append\", breaker_failure=True)"), tests=T_SHEETS),
    M("Sheets a lost worker is not a try that may have written", SHT, ("        may_have_written = ctx.maybe_acted_before or ctx.prior_claim_lost", "        may_have_written = ctx.maybe_acted_before"), tests=T_SHEETS),
    M("Sheets an earlier ambiguous try is not a try that may have written", SHT, ("        may_have_written = ctx.maybe_acted_before or ctx.prior_claim_lost", "        may_have_written = ctx.prior_claim_lost"), tests=T_SHEETS),
    M("Sheets a lost claim still waits sixty seconds", SHT, ("        if ctx.prior_claim_lost:\n            return True\n", "        if False:\n            return True\n"), tests=T_SHEETS),
    M("Sheets does not hold the append lock", SHT, ("            if not got:\n                return Outcome.retry(after_s=LOCK_RETRY_S", "            if False:\n                return Outcome.retry(after_s=LOCK_RETRY_S"), tests=T_SHEETS),
    M("Sheets lock key is shared by every spreadsheet", SHT, ('        with ctx.lock(f"sheets:{sid}", ctx.timeout_s * 4 + 10) as got:', '        with ctx.lock("sheets", ctx.timeout_s * 4 + 10) as got:'), tests=T_SHEETS),
    M("Sheets cell is not cut to 49000 characters", SHT, ('            row.append("" if value is None else str(value)[:MAX_CELL_CHARS])', '            row.append("" if value is None else str(value))'), tests=T_SHEETS),
    M("Sheets column A may be anything", SHT, ('        if columns[0] != "event_id":', "        if False:"), tests=T_SHEETS),
    M("Sheets accepts a sheet title with a space", SHT, ('SHEET_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")', 'SHEET_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_ ]{0,99}")'), tests=T_SHEETS),
    M("Sheets accepts a sheet title with a quote", SHT, ('SHEET_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")', 'SHEET_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\'\\"]{0,99}")'), tests=T_SHEETS),
    M("Sheets accepts a key that is not RSA", SHT, ('    if not isinstance(key, rsa.RSAPrivateKey):\n        raise ValueError("the service account key must be RSA")', '    if False:\n        raise ValueError("the service account key must be RSA")'), tests=T_SHEETS),
    M("Sheets token_uri skips the base URL guard", SHT, ('            check_base_url(account.token_uri, live, "the service account token_uri")', "            pass"), tests=T_SHEETS),
    M("Sheets private key is not registered for redaction", SHT, ("        out.append(pem)\n", ""), tests=T_SHEETS),
    M("Sheets private key lines are not registered for redaction", SHT, ('        out.extend(line.strip() for line in pem.splitlines() if len(line.strip()) >= 16 and "PRIVATE KEY" not in line)', "        pass"), tests=T_SHEETS),
    M("Sheets reconcile matches a prefix of the event id", SHT, ('        return "found" if event.event_id in ids else "absent"', '        return "found" if any(event.event_id in i for i in ids) else "absent"'), tests=T_SHEETS),
    M("Sheets read-back reads the wrong column", SHT, ("{ctx.extra['sheet']}!A:A", "{ctx.extra['sheet']}!B:B"), tests=T_SHEETS),
    M("Sheets treats a failed read-back as absent", SHT, ('        if not isinstance(ids, set):\n            return "unknown"\n        return "found"', '        if not isinstance(ids, set):\n            return "absent"\n        return "found"'), tests=T_SHEETS),
    M("Sheets a forbidden read-back is not a pause", SHT, ('        if res.status == 403:\n            return Outcome.pause("forbidden (403): the sheet is not shared with the service account", status=403)', '        if False:\n            return Outcome.pause("forbidden (403): the sheet is not shared with the service account", status=403)'), tests=T_SHEETS),
    # ---- tier 2: the new fakes (a fake that accepts everything is a rubber stamp) ----------------------------------------------------------------
    M("fake Meta: unhashed email and phone rule deleted", FMETA, ('"event_id", "hashed_em_ph", "hashed_other"', '"event_id", "hashed_other"'), tests=T_CAPI),
    M("fake Meta: hashed fbc rule deleted", FMETA, ('"hashed_other", "hashed_fbc", "hashed_never"', '"hashed_other", "hashed_never"'), tests=T_CAPI),
    M("fake Meta: unhashed name, city, state, zip and country rule deleted", FMETA, ('"hashed_em_ph", "hashed_other", "hashed_fbc"', '"hashed_em_ph", "hashed_fbc"'), tests=T_CAPI),
    M("fake Meta: hashed ip, user agent and fbp rule deleted", FMETA, ('"hashed_fbc", "hashed_never", "event_time_int"', '"hashed_fbc", "event_time_int"'), tests=T_CAPI),
    M("fake Meta: missing event_id rule deleted", FMETA, ('self.rules = {"event_id", "hashed_em_ph"', 'self.rules = {"hashed_em_ph"'), tests=T_CAPI),
    M("fake Meta: integer event_time rule deleted", FMETA, ('"hashed_never", "event_time_int", "event_time_age"', '"hashed_never", "event_time_age"'), tests=T_CAPI),
    M("fake Meta: 7 day event_time rule deleted", FMETA, ('"event_time_int", "event_time_age", "action_source"', '"event_time_int", "action_source"'), tests=T_CAPI),
    M("fake Meta: action_source rule deleted", FMETA, ('"event_time_age", "action_source", "website_ua"', '"event_time_age", "website_ua"'), tests=T_CAPI),
    M("fake Meta: website needs a user agent rule deleted", FMETA, ('"action_source", "website_ua"}', '"action_source"}'), tests=T_CAPI),
    M("fake Meta: duplicate event_id and name is not kept once", FMETA, ("                if kept is not None and self.clock() - kept < DEDUPE_WINDOW_S:\n                    continue", "                if False:\n                    continue"), tests=T_CAPI),
    M("fake Meta: the access token is not checked", FMETA, ('        if request.args.get("access_token") != self.token:', "        if False:"), tests=T_CAPI),
    M("fake Meta: Purchase needs no currency and value", FMETA, ('        if ev["event_name"] == "Purchase":', "        if False:"), tests=T_CAPI),
    M("fake Sheets: the JWT signature is not verified", FSHEETS, ('self.rules = {"signature", "grant_type"', 'self.rules = {"grant_type"'), tests=T_SHEETS),
    M("fake Sheets: the grant type is not checked", FSHEETS, ('"signature", "grant_type", "no_padding"', '"signature", "no_padding"'), tests=T_SHEETS),
    M("fake Sheets: padding in the JWT is accepted", FSHEETS, ('"grant_type", "no_padding", "claims"', '"grant_type", "claims"'), tests=T_SHEETS),
    M("fake Sheets: the claims are not checked", FSHEETS, ('"no_padding", "claims", "lifetime"', '"no_padding", "lifetime"'), tests=T_SHEETS),
    M("fake Sheets: the 65 minute lifetime is not checked", FSHEETS, ('"claims", "lifetime", "clock"', '"claims", "clock"'), tests=T_SHEETS),
    M("fake Sheets: the clock is not checked", FSHEETS, ('"lifetime", "clock", "raw"}', '"lifetime", "raw"}'), tests=T_SHEETS),
    M("fake Sheets: valueInputOption is not required", FSHEETS, ('"clock", "raw"}', '"clock"}'), tests=T_SHEETS),
    M("fake Sheets: an expired token is accepted", FSHEETS, ("        return expiry is not None and expiry > self.clock()", "        return expiry is not None"), tests=T_SHEETS),
    M("fake Sheets: an append goes to the first table instead of the last", FSHEETS, ("        return max(tables, key=lambda t: (t[2], t[3])) if tables else None", "        return min(tables, key=lambda t: (t[2], t[3])) if tables else None"), tests=T_SHEETS),
    M("fake Sheets: RAW turns an equals sign into a formula", FSHEETS, ('                if input_option == "USER_ENTERED" and isinstance(v, str) and v.startswith("="):', '                if isinstance(v, str) and v.startswith("="):'), tests=T_SHEETS),
    # ---- tier 2: the pass threads give their database connections back ---------------------------------------------------------------------
    M("a pass thread keeps its database connection", WRK, ("        finally:\n            self.store.release_connection()", "        finally:\n            pass"), tests=["tests/test_worker.py"]),
    M("release_connection keeps the connection in the registry", STORE, ("            if conn in self._conns:\n                self._conns.remove(conn)\n        try:\n            conn.close()", "            pass\n        try:\n            conn.close()"), tests=["tests/test_worker.py"]),
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
                return judge(targeted, mutant.stores, "")
            full = pytest(root, [], mutant.stores)
            if full.returncode != 0:
                return judge(full, mutant.stores, "only by the full suite: ")
        except subprocess.TimeoutExpired:
            return "BROKEN", f"timed out after {RUN_TIMEOUT_S}s: a hang is not a named failing test"
        return "SURVIVED", "the whole suite stayed green"


# A parameter id can hold spaces ("test_x[(650) 555-1212-None-None]"), so the id runs to the closing bracket and not to the
# first space. Pytest appends the failure message after the id, and that is not part of the id.
FAILED_LINE = re.compile(r"^FAILED (\S+?\[.*?\](?= - |$)|\S+)", re.MULTILINE)
PRISTINE: Path | None = None  # an unmutated copy, made once per run; a catching test must pass there


def judge(result: subprocess.CompletedProcess, stores: str, prefix: str) -> tuple[str, str]:
    """A mutant counts as caught only when a NAMED TEST FAILS for the mutated behaviour: not when the tree fails to
    import or collect, not when a fixture errors, not when pytest cannot find a file. The named test must also pass on
    the unmutated tree, otherwise it was red for some other reason and caught nothing."""
    out = result.stdout
    if COLLECTION_ERROR.search(out):
        return "BROKEN", "the mutated tree does not collect (an import error is not a catch)"
    named = FAILED_LINE.search(out)
    if named is None:
        return "BROKEN", "pytest failed without a failing test (a setup error or a missing file is not a catch): " + first_failure(out)
    node = named.group(1)
    assert PRISTINE is not None
    again = pytest(PRISTINE, [node], stores)
    if again.returncode != 0:
        return "BROKEN", f"{node} also fails on the unmutated tree, so it proves nothing"
    return "caught", prefix + "by " + node


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
    ap.add_argument("--shard", default="1/1", help="i/n: run every n-th mutant starting at i, so CI can split the list")
    ap.add_argument("--no-baseline", action="store_true", help="skip the unmutated runs (CI has just run the whole suite)")
    args = ap.parse_args()

    selected = [m for m in MUTANTS if args.only in m.name or args.only in m.file]
    index, count = (int(x) for x in args.shard.split("/"))
    selected = selected[index - 1 :: count]
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

    global PRISTINE
    problems = 0
    not_run = 0
    with tempfile.TemporaryDirectory(prefix="relay-pristine-") as pristine_dir:
        PRISTINE = copy_tree(Path(pristine_dir))
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for mutant, (status, detail) in zip(selected, pool.map(run_one, selected)):
                print(f"{status:<9} {mutant.name}  ({detail})", flush=True)
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
