"""Fan-out: ingest an event once, then run each (event, sink) delivery independently.

The request path only stores (ingest). Sending happens in the worker through `run_one`, one claimed row at a time, so a
slow vendor never holds a request open and one sink's failure never touches another sink's row.

Retry accounting, the part most easily gotten wrong:
  RETRY      the vendor did not act. Spends NO attempt budget. A lead goes dead only by age (72 h, per sink).
  AMBIGUOUS  the vendor may have acted. Spends the budget (6), because every try can duplicate. The tries are spread over
             at least 4.5 hours (see ambiguous_backoff_s) so an ordinary vendor incident does not spend them.
  PAUSE      a tenant level fault (revoked hook, 403). The lead stays pending, the sink pauses for that tenant.
  HELD       opt in per sink (`hold_unresolved`), for a vendor with no idempotency key and no lookup (Slack, Make). An
             AMBIGUOUS answer, or a row whose earlier claim may have posted (the worker died before its final write), is
             NOT sent again: it is stored dead with a `held:` reason and an alert, and an operator replays it after
             looking at the destination. Without the option those sinks retry and can duplicate.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Callable

from . import contacts, ghl, textsafe
from .config import Config, SinkCfg
from .sinks.base import AMBIGUOUS, DEAD, DONE, PAUSE, RETRY, SKIPPED, Event, Outcome, Sink, SinkContext
from .store.base import Claim, FinishPlan, IngestResult, SqlStore

log = logging.getLogger("relay.fanout")

MAX_ATTEMPTS = 6
EXPONENT_CAP = 30   # no backoff here needs more than 2 ** 30; an uncapped exponent past 1023 raises OverflowError when multiplied by a float
BACKOFF_BASE_S = 60.0
BACKOFF_CAP_S = 3600.0
# An AMBIGUOUS try may have posted, so its budget stays 6 (every extra try is a possible duplicate). The six tries are
# spread instead: five waits of at least 30, 60, 60, 60 and 60 minutes, a floor of 4.5 hours, so a vendor incident of up
# to 4 hours ends in a delivered alert and not a dead one. Jitter only adds time, so the floor holds.
AMBIGUOUS_BACKOFF_BASE_S = 1800.0
AMBIGUOUS_BACKOFF_CAP_S = 3600.0
AMBIGUOUS_JITTER = 0.25
BREAKER_THRESHOLD = 5
BREAKER_OPEN_S = 60.0
LEASE_MIN_S = 300.0
LEASE_TIMEOUT_FACTOR = 3.0
MAX_REASON_CHARS = 200


class Unrouted(Exception):
    """The event cannot be routed to a tenant with at least one enabled sink. The caller answers 503 and alerts."""


def route(cfg: Config, payload: dict[str, Any]) -> str:
    tenant = cfg.tenant_for_location(ghl.location_id(payload))
    if tenant is None:
        raise Unrouted("unmapped location")
    return tenant


def ingest(
    store: SqlStore,
    cfg: Config,
    *,
    tenant: str,
    key: str,
    basis: str,
    source: str,
    payload_json: str,
) -> IngestResult:
    """Store the event and freeze its sink set, in one transaction. Raises Unrouted when no sink is enabled."""
    tcfg = cfg.tenants.get(tenant)
    sinks = tcfg.enabled_sinks() if tcfg else []
    if not sinks:
        raise Unrouted("tenant has no enabled sink")
    window = tcfg.dedupe_window_s if basis in ghl.DERIVED_BASES else None
    return store.ingest(
        key=key, tenant=tenant, source=source, basis=basis, payload=payload_json, sinks=sinks, window_s=window
    )


def backoff_s(n: int, rng: random.Random) -> float:
    """min(60 * 2^n, 3600) seconds, with jitter so a recovered vendor is not hit by every row at the same second."""
    base = min(BACKOFF_BASE_S * (2 ** min(max(n, 0), EXPONENT_CAP)), BACKOFF_CAP_S)   # the exponent is capped: 2 ** 1024 no longer converts to a float
    return base / 2 + rng.random() * base / 2


def ambiguous_backoff_s(n: int, rng: random.Random) -> float:
    """The wait after the n-th ambiguous try (n from 0): min(1800 * 2^n, 3600) seconds plus up to 25 percent, never less
    than the base, so the five waits between six tries total at least 16200 seconds (4.5 hours)."""
    base = min(AMBIGUOUS_BACKOFF_BASE_S * (2 ** min(max(n, 0), EXPONENT_CAP)), AMBIGUOUS_BACKOFF_CAP_S)
    return base * (1 + AMBIGUOUS_JITTER * rng.random())


@dataclass
class Hooks:
    """Fault injection for the crash window tests. Both stay None in production."""

    before_send: Callable[[Claim], None] | None = None
    after_send: Callable[[Claim, Outcome], None] | None = None


@dataclass
class RunContext:
    store: SqlStore
    cfg: Config
    registry: dict[str, Sink]
    http: Any
    rng: random.Random = field(default_factory=random.Random)
    hooks: Hooks = field(default_factory=Hooks)


def lease_for(cfg: Config, sink: str, default_timeout_s: float) -> float:
    timeouts = [t.sinks[sink].timeout_s for t in cfg.tenants.values() if sink in t.sinks] or [default_timeout_s]
    floor = LEASE_MIN_S if cfg.lease_min_s is None else cfg.lease_min_s
    return max(floor, LEASE_TIMEOUT_FACTOR * max(timeouts))


def plan_for(outcome: Outcome, claim: Claim, scfg: SinkCfg | None, rng: random.Random, sent: bool) -> FinishPlan:
    """Turns an outcome into the one final write. Pure, so the retry arithmetic is testable on its own."""
    kind = outcome.kind
    min_interval = scfg.min_interval_s if (sent and scfg) else None
    reason = (outcome.reason or "")[:MAX_REASON_CHARS] or None
    plan = FinishPlan(
        state="pending", status=outcome.status, error=reason, effect_ref=outcome.effect_ref,
        note=reason, min_interval_s=min_interval, maybe_acted=outcome.maybe_acted,
        breaker_threshold=BREAKER_THRESHOLD, breaker_open_s=BREAKER_OPEN_S,
    )
    if kind == DONE:
        plan.state, plan.attempts_inc, plan.ok, plan.error = "done", 1, True, None
        plan.breaker = "ok" if sent else None
        return plan
    if kind == SKIPPED:
        plan.state = "skipped"
        if outcome.alert:
            plan.alerts.append(("skipped", reason or "skipped"))
        return plan
    if kind == DEAD:
        # A delivery that died without a try (expired, sink disabled, payload purged) spent no attempt.
        plan.state, plan.attempts_inc = "dead", 1 if sent else 0
        plan.alerts.append(("dead", reason or "dead"))
        return plan

    # Everything below is non terminal: RETRY, AMBIGUOUS, PAUSE.
    if kind == RETRY:
        plan.retries_inc = 1
        plan.delay_s = outcome.after_s if outcome.after_s is not None else backoff_s(claim.retries, rng)
        if outcome.rate_limited:
            plan.rate_limit_s = plan.delay_s
    elif kind == AMBIGUOUS:
        plan.attempts_inc = 1
        plan.delay_s = ambiguous_backoff_s(claim.attempts, rng)
    elif kind == PAUSE:
        plan.retries_inc = 1
        pause_s = outcome.pause_s or (scfg.pause_s if scfg else 900.0)
        plan.delay_s, plan.pause_s, plan.pause_reason = pause_s, pause_s, reason
        plan.alerts.append(("pause", reason or "paused"))
    else:  # an unknown kind is a sink bug, never a silent success
        plan.state, plan.attempts_inc, plan.error = "dead", 1, f"unknown outcome kind"
        plan.alerts.append(("dead", "unknown outcome kind"))
        return plan
    if outcome.breaker_failure:
        plan.breaker = "fail"
    if claim.attempts + plan.attempts_inc >= MAX_ATTEMPTS:
        plan.state, plan.delay_s = "dead", 0.0
        plan.error = f"attempt budget spent after {claim.attempts + plan.attempts_inc} tries"
        plan.alerts.append(("dead", plan.error))
    return plan


def _sink_cfg(cfg: Config, tenant: str, sink: str) -> SinkCfg | None:
    t = cfg.tenants.get(tenant)
    s = t.sinks.get(sink) if t else None
    return s if (s is not None and s.enabled) else None


def run_one(ctx: RunContext, claim: Claim) -> Outcome:
    """Run one claimed delivery to its final write. One bad lead never stops the loop: any exception from a sink
    becomes DEAD with the class name on the FIRST occurrence."""
    scfg = _sink_cfg(ctx.cfg, claim.tenant, claim.sink)
    sink = ctx.registry.get(claim.sink)
    sent = False
    outcome: Outcome
    event_row = ctx.store.get_event(claim.event_id)
    if scfg is None or sink is None:
        outcome = Outcome.dead("sink disabled")
    elif claim.age_s > scfg.max_age_s:
        outcome = Outcome.dead(f"expired: older than {int(scfg.max_age_s // 3600)} hours")
    elif event_row is None or event_row["payload"] is None:
        outcome = Outcome.dead("payload purged")
    else:
        if ctx.hooks.before_send is not None:
            ctx.hooks.before_send(claim)
        outcome, sent = _attempt(ctx, claim, scfg, sink, event_row)
    if ctx.hooks.after_send is not None:
        ctx.hooks.after_send(claim, outcome)
    plan = plan_for(outcome, claim, scfg, ctx.rng, sent)
    if ctx.store.finish(claim, plan):
        log.info("delivery %s %s %s status=%s", claim.event_id[:12], claim.sink, outcome.kind, outcome.status)
    else:
        log.warning("delivery %s %s lost its lease, final write ignored", claim.event_id[:12], claim.sink)
    return outcome


def _attempt(ctx: RunContext, claim: Claim, scfg: SinkCfg, sink: Sink, event_row: dict[str, Any]) -> tuple[Outcome, bool]:
    try:
        payload = json.loads(event_row["payload"])
    except ValueError:
        return Outcome.dead("stored payload is not JSON"), False
    textsafe.scrub(payload)  # a lone half emoji becomes U+FFFD before ANY sink sees the payload or the lead built from it
    event = Event(
        event_id=claim.event_id,
        tenant=claim.tenant,
        source=event_row["source"],
        payload=payload if isinstance(payload, dict) else {},
        lead=ghl.normalise_lead(payload),
        received_epoch=event_row.get("received_epoch"),
    )
    sctx = SinkContext(
        tenant=claim.tenant, url=scfg.url, timeout_s=scfg.timeout_s, apikey=scfg.apikey,
        attempts=claim.attempts, retries=claim.retries, extra=dict(scfg.extra),
        maybe_acted_before=claim.maybe_acted_before, since_maybe_acted_s=claim.since_maybe_acted_s,
        prior_claim_lost=claim.prior_claim_lost, lock=ctx.store.append_lock,
    )
    refused = _contact_refusal(ctx, claim, event)
    if refused is not None:
        return refused, False
    holds = scfg.hold_unresolved and not sink.can_reconcile
    if holds and (claim.maybe_acted_before or claim.prior_claim_lost):
        # An earlier try may have posted (an ambiguous answer, or a worker that died before its final write) and this
        # vendor cannot be asked, so sending again could message the same person twice: nothing is sent.
        return Outcome.held("an earlier try may have been sent and this destination cannot confirm it"), False
    try:
        outcome = sink.send(event, ctx.http, sctx)
        if outcome.kind == AMBIGUOUS and sink.can_reconcile:
            outcome = _resolve(sink, event, ctx.http, sctx, outcome)
        elif outcome.kind == AMBIGUOUS and holds:
            outcome = Outcome.held("the destination may have accepted it and cannot confirm", status=outcome.status)
        return outcome, outcome.kind != SKIPPED
    except Exception as exc:
        log.error("sink %s raised %s", claim.sink, type(exc).__name__, exc_info=True)
        return Outcome.dead(f"sink bug: {type(exc).__name__}"), True


def _contact_refusal(ctx: RunContext, claim: Claim, event: Event) -> Outcome | None:
    """The send time check: a suppressed contact, or one already sent this message kind by a history import, is never sent
    to. The reason is stored on the delivery. When the lists cannot be read the row is retried and NOT sent: an unreadable
    do-not-contact list is never read as an empty one."""
    keys = contacts.lead_keys(event.lead, ctx.cfg.default_region)
    if not keys:
        return None
    kind = event.lead.get("event_type") or "event"
    try:
        hit = ctx.store.contact_block(keys, kind)
    except Exception as exc:
        log.error("contact check for %s failed: %s", claim.sink, type(exc).__name__)
        return Outcome.retry(reason=f"contact check failed: {type(exc).__name__}")
    if hit is None:
        return None
    what, why = hit
    return Outcome.suppressed(f"suppressed: {why}" if what == "suppressed" else why)


def _resolve(sink: Sink, event: Event, http: Any, sctx: SinkContext, outcome: Outcome) -> Outcome:
    """An ambiguous send on a sink that can read back: found means it landed, absent means it did not."""
    found = sink.reconcile(event, http, sctx)
    if found == "found":
        return Outcome.done(status=outcome.status, effect_ref="reconciled")
    if found == "absent":
        # Nothing there yet, but a vendor can land a request late, so the next try must look again before it sends.
        absent = Outcome.retry(status=outcome.status, reason="reconciled absent", breaker_failure=True)
        absent.maybe_acted = True
        return absent
    return outcome


def process_next(ctx: RunContext, sink_name: str) -> bool:
    """Claim and run ONE row for a sink. False when nothing is claimable."""
    sink = ctx.registry.get(sink_name)
    default_timeout = sink.default_timeout_s if sink else 10.0
    claim = ctx.store.claim(sink_name, lease_for(ctx.cfg, sink_name, default_timeout))
    if claim is None:
        return False
    run_one(ctx, claim)
    return True
