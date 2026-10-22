"""Builders shared by the suites: a config over the local fakes, lead payloads, and a harness that runs a worker on the
database clock so no test ever sleeps."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from typing import Any

from relay import ghl
from relay.config import Config, load_config
from relay.fanout import Hooks, ingest
from relay.http import Http
from relay.store.base import SqlStore
from relay.worker import Worker

INGEST_TOKEN = "ingest-token-0123456789"
PREVIOUS_TOKEN = "previous-token-0123456789"
ADMIN_TOKEN = "admin-token-0123456789ab"
WORKFLOW_SECRET = "workflow-secret-0123456789"
LOCATION = "LOC1"
TENANT = "acme"


class SimulatedCrash(BaseException):
    """Derives from BaseException so run_one's catch-all (Exception) cannot swallow it: it models the process dying."""


def env_for(slack_url: str | None, make_url: str | None, **extra: str) -> dict[str, str]:
    env = {
        "RELAY_ENV": "test",
        "RELAY_INGEST_TOKEN": INGEST_TOKEN,
        "RELAY_ADMIN_TOKEN": ADMIN_TOKEN,
        "ACME_WORKFLOW_SECRET": WORKFLOW_SECRET,
    }
    if slack_url:
        env["ACME_SLACK_URL"] = slack_url
    if make_url:
        env["ACME_MAKE_URL"] = make_url
    env.update(extra)
    return env


def config_json(
    *, slack: bool = True, make: bool = True, slack_timeout: float = 5.0, make_timeout: float = 5.0, tenant_extra: dict | None = None,
    slack_extra: dict | None = None, make_extra: dict | None = None, second_tenant: bool = False, extra_sinks: dict | None = None,
    top_extra: dict | None = None,
) -> str:
    sinks: dict[str, Any] = {}
    if slack:
        sinks["slack"] = {"url_env": "ACME_SLACK_URL", "timeout_s": slack_timeout, **(slack_extra or {})}
    if make:
        sinks["make"] = {"url_env": "ACME_MAKE_URL", "timeout_s": make_timeout, **(make_extra or {})}
    sinks.update(extra_sinks or {})
    tenants: dict[str, Any] = {TENANT: {"workflow_secret_env": "ACME_WORKFLOW_SECRET", "sinks": sinks, **(tenant_extra or {})}}
    return json.dumps({"locations": {LOCATION: TENANT}, "tenants": tenants, **(top_extra or {})})


def make_cfg(slack_url: str | None = None, make_url: str | None = None, role: str = "web", env_extra: dict | None = None, **kw: Any) -> Config:
    env = env_for(slack_url, make_url, **(env_extra or {}))
    env["RELAY_CONFIG"] = config_json(slack=slack_url is not None, make=make_url is not None, **kw)
    return load_config(env, role=role)


def payload(i: int = 1, **over: Any) -> dict[str, Any]:
    """A ContactCreate shaped lead, from the saved marketplace example."""
    base = {
        "type": "ContactCreate",
        "locationId": LOCATION,
        "id": f"contact{i:05d}",
        "firstName": "Pat",
        "lastName": f"Lead{i}",
        "name": f"Pat Lead{i}",
        "email": f"pat{i}@example.com",
        "phone": "+14155550100",
        "city": "Austin",
        "state": "TX",
        "source": "roof form",
        "dateAdded": "2026-10-20T12:41:02.193Z",
        "dateUpdated": "2026-10-20T12:41:03.193Z",
        "tags": ["hail"],
    }
    base.update(over)
    return base


def raw_of(p: dict[str, Any]) -> bytes:
    return json.dumps(p).encode("utf-8")


def ingest_one(store: SqlStore, cfg: Config, p: dict[str, Any] | None = None, key: str | None = None):
    p = p if p is not None else payload()
    key = key or hashlib.sha256(raw_of(p)).hexdigest()
    return ingest(store, cfg, tenant=TENANT, key=key, basis=ghl.BASIS_NATIVE, source="ingest", payload_json=json.dumps(p))


@dataclass
class Rig:
    store: SqlStore
    cfg: Config
    worker: Worker
    http: Http

    def settle(self, max_rounds: int = 400, jump_s: float | None = None) -> int:
        """Run the worker until nothing is pending, moving the DATABASE clock to the next due moment instead of waiting.
        Returns the number of rows processed."""
        total = 0
        for _ in range(max_rounds):
            total += self.worker.run_once()
            due = self.store.due_in_s()
            if due is None:
                return total
            self.store.advance(jump_s if jump_s is not None else max(due, 0.05) + 0.01)
        raise AssertionError("did not settle: pending deliveries remain")

    def states(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.store.deliveries():
            out[d["state"]] = out.get(d["state"], 0) + 1
        return out


def make_rig(store: SqlStore, cfg: Config, *, seed: int = 1, hooks: Hooks | None = None, registry: dict | None = None,
             per_sink_cap: int = 5) -> Rig:
    http = Http()
    worker = Worker(
        cfg, store, http, rng=random.Random(seed), sleeper=store.advance, hooks=hooks, registry=registry,
        per_sink_cap=per_sink_cap,
    )
    return Rig(store=store, cfg=cfg, worker=worker, http=http)


def wait_for(cond, timeout: float = 15.0, what: str = "condition") -> None:
    """Poll a condition in 10 ms steps up to a deadline. A wait on the event itself, never a fixed sleep."""
    import threading
    import time

    ticker = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        ticker.wait(0.01)
    raise AssertionError(f"timed out waiting for {what}")


from relay.sinks.base import Event, Outcome, Sink, SinkContext  # noqa: E402


class ScriptSink(Sink):
    """A sink that returns scripted outcomes (or raises) and records what it was asked. For the machinery tests."""

    def __init__(self, name: str = "slack", outcomes: list | None = None, can_reconcile: bool = False, reconcile_result: str = "unknown"):
        self.name = name
        self.outcomes = list(outcomes or [])
        self.calls: list[str] = []
        self.can_reconcile = can_reconcile
        self.reconcile_result = reconcile_result

    def send(self, event: Event, http, ctx: SinkContext) -> Outcome:
        self.calls.append(event.event_id)
        step = self.outcomes.pop(0) if self.outcomes else Outcome.done()
        if isinstance(step, BaseException):
            raise step
        return step

    def reconcile(self, event: Event, http, ctx: SinkContext) -> str:
        if isinstance(self.reconcile_result, BaseException):
            raise self.reconcile_result
        return self.reconcile_result
