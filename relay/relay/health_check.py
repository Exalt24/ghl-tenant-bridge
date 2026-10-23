"""An alert when jobs stop.

    python -m relay.health_check                          prints the findings, exits 0 when healthy and 1 when not
    python -m relay.health_check --alert                  also posts ONE Slack alert per cooldown to the alert webhook

Two separate findings, each with its own message:
  heartbeat   the worker writes a row with the time on every cycle; one older than --max-heartbeat-age-s (or none at all, or one
              that cannot be read) means the worker is not running
  backlog     the oldest delivery that is still pending is older than --max-oldest-pending-s: the worker runs but work is stuck

A read that fails is a finding, never a pass. The alert goes through the Slack sink (`SlackSink.send_alert`) to the config's alert
webhook, and a cooldown kept in the database (`alert_state`, key `health`) lets one outage alert once per cooldown, however often
this command is scheduled. A failed post reopens the gate after a minute. When the database itself cannot be read there is no place
to keep the cooldown, so no alert is posted: the exit code and the printed finding are the signal, and the scheduler that runs this
should alert on a non-zero exit.

Limits come from the flags, then RELAY_HEALTH_MAX_HEARTBEAT_AGE_S, RELAY_HEALTH_MAX_OLDEST_PENDING_S and
RELAY_HEALTH_ALERT_COOLDOWN_S. Exit codes: 0 healthy, 1 findings, 2 bad setup.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from dataclasses import dataclass
from typing import Any, Mapping

from . import redact
from .config import Config, ConfigError, load_config
from .http import Http
from .sinks.base import DONE, SinkContext
from .sinks.slack import SlackSink, escape
from .store import open_store
from .store.base import SqlStore

log = logging.getLogger("relay.health_check")

DEFAULT_MAX_HEARTBEAT_AGE_S = 120.0
DEFAULT_MAX_OLDEST_PENDING_S = 3600.0
DEFAULT_ALERT_COOLDOWN_S = 3600.0
RETRY_AFTER_FAILURE_S = 60.0
POST_TIMEOUT_S = 5.0
GATE = "health"
BEATER = "worker"


@dataclass(frozen=True)
class Finding:
    code: str  # "heartbeat" | "backlog" | "database"
    message: str


def _seconds(value: float) -> str:
    return f"{value:.0f}s"


def check(store: SqlStore, max_heartbeat_age_s: float, max_oldest_pending_s: float) -> list[Finding]:
    findings: list[Finding] = []
    try:
        age = store.heartbeat_age_s(BEATER)
    except Exception as exc:
        findings.append(Finding("heartbeat", f"the worker heartbeat could not be read ({type(exc).__name__}), so it is treated as missing"))
    else:
        if age is None:
            findings.append(Finding("heartbeat", "the worker has never written a heartbeat: it is not running, or it never started"))
        elif not (age <= max_heartbeat_age_s):  # written so that a NaN age is a finding, not a pass
            findings.append(Finding("heartbeat", f"the worker heartbeat is {_seconds(age)} old, the limit is {_seconds(max_heartbeat_age_s)}: the worker has stopped"))
    try:
        oldest = store.oldest_unfinished_age_s()
    except Exception as exc:
        findings.append(Finding("backlog", f"the oldest unfinished delivery could not be read ({type(exc).__name__}), so it is treated as stuck"))
    else:
        if oldest is not None and not (oldest <= max_oldest_pending_s):
            findings.append(Finding("backlog", f"the oldest unfinished delivery is {_seconds(oldest)} old, the limit is {_seconds(max_oldest_pending_s)}: work is stuck"))
    return findings


def alert_text(findings: list[Finding]) -> str:
    return escape("relay health alert: " + " | ".join(f"{f.code}: {f.message}" for f in findings))


def send_alert(store: SqlStore, http: Any, cfg: Config, findings: list[Finding], cooldown_s: float) -> str:
    """One alert per cooldown. Returns sent | cooldown | no_webhook | failed | gate_unreadable."""
    text = alert_text(findings)
    if not cfg.alert_url:
        log.warning("RELAY_ALERT %s", text)
        return "no_webhook"
    try:
        if not store.alert_gate(GATE, cooldown_s):
            return "cooldown"
    except Exception as exc:
        log.error("health alert cooldown could not be read: %s", type(exc).__name__)
        return "gate_unreadable"
    ctx = SinkContext(tenant="relay", url=cfg.alert_url, timeout_s=POST_TIMEOUT_S)
    try:
        outcome = SlackSink().send_alert(text, http, ctx)
    except Exception as exc:
        log.warning("health alert not delivered: %s", type(exc).__name__)
        outcome = None
    if outcome is not None and outcome.kind == DONE:
        return "sent"
    try:
        store.alert_gate_release(GATE, cooldown_s, RETRY_AFTER_FAILURE_S)
    except Exception as exc:
        log.error("health alert gate could not be reopened: %s", type(exc).__name__)
    return "failed"


def _limit(flag: float | None, env: Mapping[str, str], name: str, default: float) -> float:
    if flag is not None:
        value = flag
    elif env.get(name):
        try:
            value = float(env[name])
        except ValueError:
            raise ConfigError(f"{name} must be a number") from None
    else:
        value = default
    if not (value > 0) or math.isinf(value):
        raise ConfigError(f"{name} must be a positive number of seconds")
    return value


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None, http: Any = None, store: SqlStore | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay.health_check", description=__doc__.split("\n")[0])
    parser.add_argument("--max-heartbeat-age-s", type=float, default=None)
    parser.add_argument("--max-oldest-pending-s", type=float, default=None)
    parser.add_argument("--alert", action="store_true", help="post one Slack alert per cooldown when something is wrong")
    parser.add_argument("--cooldown-s", type=float, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = load_config(environ, role="worker")
        env = os.environ if environ is None else environ
        max_hb = _limit(args.max_heartbeat_age_s, env, "RELAY_HEALTH_MAX_HEARTBEAT_AGE_S", DEFAULT_MAX_HEARTBEAT_AGE_S)
        max_old = _limit(args.max_oldest_pending_s, env, "RELAY_HEALTH_MAX_OLDEST_PENDING_S", DEFAULT_MAX_OLDEST_PENDING_S)
        cooldown = _limit(args.cooldown_s, env, "RELAY_HEALTH_ALERT_COOLDOWN_S", DEFAULT_ALERT_COOLDOWN_S)
    except ConfigError as exc:
        print(f"relay.health_check: configuration refused: {exc}", file=sys.stderr)
        return 2
    redact.install()
    try:
        store = store or open_store(cfg.database_url, cfg.db_path)
    except Exception as exc:
        print(f"FINDING database: the database could not be opened ({type(exc).__name__})")
        return 1
    findings = check(store, max_hb, max_old)
    if not findings:
        print("OK: the worker heartbeat is fresh and no delivery is stuck")
        return 0
    for f in findings:
        print(f"FINDING {f.code}: {f.message}")
    if args.alert:
        own_http = http is None
        client = http or Http()
        try:
            print(f"alert: {send_alert(store, client, cfg, findings, cooldown)}")
        finally:
            if own_http:
                client.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
