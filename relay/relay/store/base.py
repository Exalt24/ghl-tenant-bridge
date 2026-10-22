"""Store logic shared by SQLite and Postgres.

The SQL is written once with `?` placeholders and two dialect hooks (`now()` and the claim lock clause). Every time
value comes from the DATABASE clock: SQLite's `strftime('now')`, Postgres's `clock_timestamp()`. The Python process
clock is never consulted, so a worker with a skewed clock cannot shorten or extend a lease. Tests move time with
`advance()`, which shifts the offset the database expression adds; production never calls it.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

CLAIMABLE_STATES = ("pending",)
ALERT_CAP_PER_SINK = 200


@dataclass
class IngestResult:
    event_id: str
    duplicate: bool


@dataclass
class Claim:
    event_id: str
    sink: str
    tenant: str
    attempts: int
    retries: int
    token: str
    age_s: float
    # Set when a try before this one may have reached the vendor: an ambiguous send, or a read-back that found nothing
    # yet. `since_maybe_acted_s` is how long ago, on the database clock.
    maybe_acted_before: bool = False
    since_maybe_acted_s: float | None = None
    # An earlier claim ended with no final write (the worker died): that try may have reached the vendor too.
    prior_claim_lost: bool = False


@dataclass
class FinishPlan:
    """Everything one final write needs, decided by the caller, applied in ONE transaction."""

    state: str  # pending | done | dead | skipped
    delay_s: float = 0.0
    attempts_inc: int = 0
    retries_inc: int = 0
    status: int | None = None
    error: str | None = None
    effect_ref: str | None = None
    ok: bool = False
    note: str | None = None
    min_interval_s: float | None = None
    rate_limit_s: float | None = None
    pause_s: float | None = None
    pause_reason: str | None = None
    maybe_acted: bool = False
    breaker: str | None = None  # 'ok' | 'fail' | None
    breaker_threshold: int = 5
    breaker_open_s: float = 60.0
    alerts: list[tuple[str, str]] = field(default_factory=list)  # (kind, reason)


def to_epoch(value: Any) -> float:
    """Seconds since the Unix epoch from what a store hands back: a datetime (Postgres) or the UTC text SQLite keeps."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc).timestamp()


class SqlStore:
    """Subclasses supply the connection, `tx()`, `now()` and the claim lock clause."""

    dialect = "base"
    skip_locked = ""

    def __init__(self) -> None:
        self.offset_s = 0.0
        self.faults: dict[str, Callable[[], None]] = {}
        self._local = threading.local()
        self._conns: list[Any] = []
        self._conns_lock = threading.Lock()

    # ---- dialect hooks -------------------------------------------------------------------------------------------
    def now(self, plus_s: float = 0.0) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def age_s(self, col: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def epoch(self, expr: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    greatest = "GREATEST"

    def ts_param(self, dt: datetime) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def tx(self, write: bool = True):  # pragma: no cover - interface
        raise NotImplementedError

    def init_schema(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    # ---- test seams ----------------------------------------------------------------------------------------------
    def advance(self, seconds: float) -> None:
        """Tests only: move the database clock forward by shifting the offset its SQL expressions add."""
        self.offset_s += seconds

    def _fault(self, name: str) -> None:
        hook = self.faults.get(name)
        if hook is not None:
            hook()

    # ---- helpers -------------------------------------------------------------------------------------------------
    @staticmethod
    def _rows(cur: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in cur.fetchall()]

    def _one(self, t: Any, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        row = t.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def _later(self, col: str, expr: str) -> str:
        """SQL that keeps the later of a column and an expression, NULL counts as earliest."""
        return f"CASE WHEN {col} IS NULL OR {col} < {expr} THEN {expr} ELSE {col} END"

    def release_connection(self) -> None:
        """Close the connection the CALLING thread opened. A pool that starts fresh threads on every pass would otherwise
        leave one open connection per thread per pass behind, and Postgres refuses clients past max_connections."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        with self._conns_lock:
            if conn in self._conns:
                self._conns.remove(conn)
        try:
            conn.close()
        except Exception:
            pass

    def ping(self) -> bool:
        with self.tx(write=False) as t:
            return t.execute("SELECT 1 AS one").fetchone() is not None

    def db_now(self) -> str:
        with self.tx(write=False) as t:
            row = t.execute(f"SELECT {self.now()} AS n").fetchone()
        return str(row["n"])

    # ---- ingest --------------------------------------------------------------------------------------------------
    def ingest(
        self,
        *,
        key: str,
        tenant: str,
        source: str,
        basis: str,
        payload: str,
        sinks: list[str],
        window_s: float | None = None,
        on_new: Callable[[Any], None] | None = None,
    ) -> IngestResult:
        """ONE transaction: the event row, one delivery row per enabled sink, or a `suppressed` row for a duplicate.

        `on_new(t)` runs inside the same transaction when (and only when) a new event row was inserted. The Fathom receiver
        uses it to store the meeting beside the event, so a crash can never leave one without the other.

        window_s None means a native key: the primary key alone decides, forever. A number means a derived key: an
        identical event inside the window is suppressed, an older one becomes a NEW event `<key>|<seq>`.
        """
        with self.tx() as t:
            seq = 0
            if window_s is not None:
                last = self._one(
                    t,
                    f"SELECT seq, {self.age_s('received_at')} AS age_s FROM events WHERE dedupe_key = ? ORDER BY seq DESC LIMIT 1",
                    (key,),
                )
                if last is not None:
                    seq = int(last["seq"]) + 1
                    if float(last["age_s"]) < window_s:
                        self._suppress(t, key, basis, tenant)
                        return IngestResult(key if int(last["seq"]) == 0 else f"{key}|{last['seq']}", True)
            event_id = key if seq == 0 else f"{key}|{seq}"
            cur = t.execute(
                f"INSERT INTO events (event_id, tenant, source, dedupe_basis, dedupe_key, seq, received_at, payload) "
                f"VALUES (?, ?, ?, ?, ?, ?, {self.now()}, ?) ON CONFLICT (event_id) DO NOTHING",
                (event_id, tenant, source, basis, key, seq, payload),
            )
            if cur.rowcount != 1:
                self._suppress(t, event_id, basis, tenant)
                return IngestResult(event_id, True)
            self._fault("ingest_after_event")
            self._add_deliveries(t, event_id, tenant, sinks)
            if on_new is not None:
                on_new(t)
            return IngestResult(event_id, False)

    def _suppress(self, t: Any, event_id: str, basis: str, tenant: str) -> None:
        t.execute(
            f"INSERT INTO suppressed (event_id, at, basis, tenant) VALUES (?, {self.now()}, ?, ?)",
            (event_id, basis, tenant),
        )

    def _add_deliveries(self, t: Any, event_id: str, tenant: str, sinks: list[str]) -> None:
        for sink in sinks:
            t.execute(
                f"INSERT INTO deliveries (event_id, sink, tenant, state, created_at, next_at) "
                f"VALUES (?, ?, ?, 'pending', {self.now()}, {self.now()})",
                (event_id, sink, tenant),
            )
            self._fault("ingest_mid_deliveries")

    # ---- claim ---------------------------------------------------------------------------------------------------
    def _claimable(self, a: str) -> str:
        n = self.now()
        return (
            f"{a}.state = 'pending' AND {a}.next_at <= {n} "
            f"AND ({a}.claim_until IS NULL OR {a}.claim_until <= {n}) "
            f"AND NOT EXISTS (SELECT 1 FROM sink_state s WHERE s.tenant = {a}.tenant AND s.sink = {a}.sink "
            f"AND (s.paused_until > {n} OR s.breaker_until > {n} OR s.next_send_at > {n}))"
        )

    def claim(self, sink: str, lease_s: float) -> Claim | None:
        """Claim ONE due row for this sink. One statement, so two workers can never hold the same row."""
        token = uuid.uuid4().hex
        with self.tx() as t:
            row = self._one(
                t,
                f"UPDATE deliveries SET claim_until = {self.now(lease_s)}, claim_token = ?, claims = claims + 1 "
                f"WHERE (event_id, sink) IN ("
                f"SELECT d.event_id, d.sink FROM deliveries d WHERE d.sink = ? AND {self._claimable('d')} "
                f"ORDER BY d.next_at, d.created_at, d.event_id LIMIT 1 {self.skip_locked}) "
                f"AND {self._claimable('deliveries')} "
                f"RETURNING event_id, sink, tenant, attempts, retries, claim_token, claims, maybe_acted_at, "
                f"{self.age_s('created_at')} AS age_s, {self.age_s('maybe_acted_at')} AS since_maybe_acted_s",
                (token, sink),
            )
        if row is None:
            return None
        return Claim(
            event_id=row["event_id"],
            sink=row["sink"],
            tenant=row["tenant"],
            attempts=int(row["attempts"]),
            retries=int(row["retries"]),
            token=row["claim_token"],
            age_s=float(row["age_s"]),
            maybe_acted_before=row["maybe_acted_at"] is not None,
            since_maybe_acted_s=None if row["since_maybe_acted_s"] is None else float(row["since_maybe_acted_s"]),
            # Every finished try adds exactly one to attempts or retries, so more earlier claims than that means one
            # of them never reached its final write.
            prior_claim_lost=int(row["claims"]) - 1 > int(row["attempts"]) + int(row["retries"]),
        )

    # ---- final write ---------------------------------------------------------------------------------------------
    def finish(self, claim: Claim, plan: FinishPlan) -> bool:
        """The conditional final write. False means the lease was lost and nothing was written.

        State change, the attempts row, the flow-control row and the alerts are ONE transaction, so the log can never
        say "delivered" while the delivery says "pending".
        """
        with self.tx() as t:
            cur = t.execute(
                f"UPDATE deliveries SET state = ?, attempts = attempts + ?, retries = retries + ?, "
                f"claims = attempts + ? + retries + ?, "
                f"next_at = {self.now(plan.delay_s)}, claim_until = NULL, claim_token = NULL, "
                f"last_status = ?, last_error = ?, effect_ref = COALESCE(?, effect_ref), "
                f"maybe_acted_at = CASE WHEN ? = 1 THEN {self.now()} ELSE maybe_acted_at END, "
                f"finished_at = CASE WHEN ? = 'pending' THEN NULL ELSE {self.now()} END "
                f"WHERE event_id = ? AND sink = ? AND claim_token = ? AND state = 'pending'",
                (
                    plan.state,
                    plan.attempts_inc,
                    plan.retries_inc,
                    # A finished try brings `claims` back in line with the tries counted, so a lost claim is remembered
                    # by the maybe_acted mark below (set when this claim found one) and not by a permanent offset.
                    plan.attempts_inc,
                    plan.retries_inc,
                    plan.status,
                    plan.error,
                    plan.effect_ref,
                    1 if (plan.maybe_acted or claim.prior_claim_lost) else 0,
                    plan.state,
                    claim.event_id,
                    claim.sink,
                    claim.token,
                ),
            )
            if cur.rowcount != 1:
                return False
            self._fault("finish_mid")
            t.execute(
                f"INSERT INTO attempts (event_id, sink, n, at, status, ok, note) "
                f"SELECT ?, ?, COALESCE(MAX(n), 0) + 1, {self.now()}, ?, ?, ? FROM attempts WHERE event_id = ? AND sink = ?",
                (claim.event_id, claim.sink, plan.status, 1 if plan.ok else 0, plan.note, claim.event_id, claim.sink),
            )
            self._update_sink_state(t, claim, plan)
            for kind, reason in plan.alerts:
                self._insert_alert(t, claim.sink, claim.tenant, kind, reason, claim.event_id)
            return True

    def _update_sink_state(self, t: Any, claim: Claim, plan: FinishPlan) -> None:
        wants = (
            plan.min_interval_s is not None
            or plan.rate_limit_s is not None
            or plan.pause_s is not None
            or plan.breaker is not None
        )
        if not wants:
            return
        t.execute(
            "INSERT INTO sink_state (tenant, sink) VALUES (?, ?) ON CONFLICT (tenant, sink) DO NOTHING",
            (claim.tenant, claim.sink),
        )
        gap = max(plan.min_interval_s or 0.0, plan.rate_limit_s or 0.0)
        if gap > 0:
            t.execute(
                f"UPDATE sink_state SET next_send_at = {self._later('next_send_at', self.now(gap))} "
                f"WHERE tenant = ? AND sink = ?",
                (claim.tenant, claim.sink),
            )
        if plan.pause_s is not None:
            t.execute(
                f"UPDATE sink_state SET paused_until = {self.now(plan.pause_s)}, reason = ? WHERE tenant = ? AND sink = ?",
                (plan.pause_reason, claim.tenant, claim.sink),
            )
        if plan.breaker == "ok":
            t.execute(
                "UPDATE sink_state SET consecutive_failures = 0, breaker_until = NULL, paused_until = NULL, reason = NULL "
                "WHERE tenant = ? AND sink = ?",
                (claim.tenant, claim.sink),
            )
        elif plan.breaker == "fail":
            t.execute(
                f"UPDATE sink_state SET consecutive_failures = consecutive_failures + 1, "
                f"breaker_until = CASE WHEN consecutive_failures + 1 >= ? THEN {self.now(plan.breaker_open_s)} "
                f"ELSE breaker_until END WHERE tenant = ? AND sink = ?",
                (plan.breaker_threshold, claim.tenant, claim.sink),
            )

    # ---- age out -------------------------------------------------------------------------------------------------
    def expire_old(self, max_age_by_sink: dict[str, float]) -> list[dict[str, Any]]:
        """Pending rows past their sink's age limit go dead, so a paused sink cannot hold a lead forever."""
        out: list[dict[str, Any]] = []
        with self.tx() as t:
            for sink, max_age in max_age_by_sink.items():
                reason = f"expired: older than {int(max_age // 3600)} hours"
                rows = self._rows(
                    t.execute(
                        f"UPDATE deliveries SET state = 'dead', last_error = ?, claim_until = NULL, claim_token = NULL, "
                        f"finished_at = {self.now()} "
                        f"WHERE sink = ? AND state = 'pending' AND (claim_until IS NULL OR claim_until <= {self.now()}) "
                        f"AND created_at <= {self.now(-max_age)} RETURNING event_id, tenant, sink",
                        (reason, sink),
                    )
                )
                for r in rows:
                    self._insert_alert(t, r["sink"], r["tenant"], "dead", reason, r["event_id"])
                out.extend(rows)
        return out

    # ---- operator actions ----------------------------------------------------------------------------------------
    def replay(self, event_id: str, sink: str | None = None, force: bool = False) -> int:
        """Put selected deliveries back to pending. Only `dead` rows unless force, which also accepts done/skipped."""
        states = ("dead", "done", "skipped") if force else ("dead",)
        marks = ", ".join("?" for _ in states)
        sql = (
            f"UPDATE deliveries SET state = 'pending', attempts = 0, retries = 0, claims = 0, maybe_acted_at = NULL, "
            f"created_at = {self.now()}, "
            f"next_at = {self.now()}, claim_until = NULL, claim_token = NULL, last_status = NULL, last_error = NULL, "
            f"finished_at = NULL WHERE event_id = ? AND state IN ({marks})"
        )
        params: list[Any] = [event_id, *states]
        if sink is not None:
            sql += " AND sink = ?"
            params.append(sink)
        with self.tx() as t:
            return t.execute(sql, tuple(params)).rowcount

    def backfill(self, tenant: str, sink: str, since: datetime) -> int:
        """Insert the delivery rows an event never got (the sink was enabled later). Existing rows are left alone."""
        with self.tx() as t:
            return t.execute(
                f"INSERT INTO deliveries (event_id, sink, tenant, state, created_at, next_at) "
                f"SELECT e.event_id, ?, e.tenant, 'pending', {self.now()}, {self.now()} FROM events e "
                f"WHERE e.tenant = ? AND e.received_at >= ? AND e.payload IS NOT NULL "
                f"AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.event_id = e.event_id AND d.sink = ?)",
                (sink, tenant, self.ts_param(since), sink),
            ).rowcount

    def purge(self, older_than_days: float = 30.0) -> int:
        """Null the stored payload of old events. The row stays, so dedupe still holds."""
        with self.tx() as t:
            return t.execute(
                f"UPDATE events SET payload = NULL WHERE payload IS NOT NULL AND received_at <= {self.now(-older_than_days * 86400.0)}"
            ).rowcount

    # ---- ad spend and meetings (tier 3) --------------------------------------------------------------------------
    def upsert_spend(self, rows: list[dict[str, Any]]) -> int:
        """Insert or refresh daily spend rows on their primary key (source, account, campaign, day), in ONE transaction, so a
        28 day re-pull replaces yesterday's figures and never stacks a second copy."""
        with self.tx() as t:
            for r in rows:
                t.execute(
                    f"INSERT INTO spend_daily (source, account, campaign_id, day, spend_minor, currency, impressions, clicks, pulled_at) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, {self.now()}) "
                    f"ON CONFLICT (source, account, campaign_id, day) DO UPDATE SET spend_minor = excluded.spend_minor, "
                    f"currency = excluded.currency, impressions = excluded.impressions, clicks = excluded.clicks, "
                    f"pulled_at = excluded.pulled_at",
                    (r["source"], r["account"], r["campaign_id"], r["day"], r["spend_minor"], r["currency"], r.get("impressions"), r.get("clicks")),
                )
        return len(rows)

    def spend_rows(self, source: str | None = None) -> list[dict[str, Any]]:
        with self.tx(write=False) as t:
            if source is None:
                return self._rows(t.execute("SELECT * FROM spend_daily ORDER BY source, account, campaign_id, day"))
            return self._rows(t.execute("SELECT * FROM spend_daily WHERE source = ? ORDER BY account, campaign_id, day", (source,)))

    def save_meeting(
        self, t: Any, *, tenant: str, recording_id: str, title: str | None, summary: str | None, action_items: str | None
    ) -> None:
        """Insert a meeting, or refresh it for the SAME tenant. A summary or action items already stored are kept when the new
        copy has none (the puller asks for less than the webhook carries). Another tenant's row is never touched."""
        t.execute(
            f"INSERT INTO fathom_meetings (recording_id, tenant, title, summary, action_items, received_at) "
            f"VALUES (?, ?, ?, ?, ?, {self.now()}) "
            f"ON CONFLICT (recording_id) DO UPDATE SET title = excluded.title, "
            f"summary = COALESCE(excluded.summary, fathom_meetings.summary), "
            f"action_items = COALESCE(excluded.action_items, fathom_meetings.action_items) "
            f"WHERE fathom_meetings.tenant = excluded.tenant",
            (recording_id, tenant, title, summary, action_items),
        )

    def upsert_meetings(self, tenant: str, meetings: list[dict[str, Any]]) -> int:
        with self.tx() as t:
            for m in meetings:
                self.save_meeting(
                    t, tenant=tenant, recording_id=m["recording_id"], title=m.get("title"), summary=m.get("summary"),
                    action_items=m.get("action_items"),
                )
        return len(meetings)

    def meetings(self) -> list[dict[str, Any]]:
        with self.tx(write=False) as t:
            return self._rows(t.execute("SELECT * FROM fathom_meetings ORDER BY recording_id"))

    # ---- one append in flight per key ----------------------------------------------------------------------------
    @contextmanager
    def append_lock(self, key: str, ttl_s: float) -> Iterator[bool]:
        """`with store.append_lock(key, ttl) as got:` yields True when this caller holds the lock and False when another
        holder has it. A lock row (SQLite) expires on the database clock, so a holder that died frees it after ttl_s."""
        holder = uuid.uuid4().hex
        got = self._lock_try(key, holder, ttl_s)
        try:
            yield got
        finally:
            if got:
                self._lock_release(key, holder)

    def _lock_try(self, key: str, holder: str, ttl_s: float) -> bool:
        with self.tx() as t:
            cur = t.execute(
                f"INSERT INTO append_locks (lock_key, holder, until) VALUES (?, ?, {self.now(ttl_s)}) "
                f"ON CONFLICT (lock_key) DO UPDATE SET holder = excluded.holder, until = excluded.until "
                f"WHERE append_locks.until <= {self.now()}",
                (key, holder),
            )
            return cur.rowcount == 1

    def _lock_release(self, key: str, holder: str) -> None:
        with self.tx() as t:
            t.execute("DELETE FROM append_locks WHERE lock_key = ? AND holder = ?", (key, holder))

    # ---- alerts --------------------------------------------------------------------------------------------------
    def _insert_alert(self, t: Any, sink: str, tenant: str | None, kind: str, reason: str, event_id: str | None) -> None:
        t.execute(
            f"INSERT INTO alerts (sink, tenant, kind, reason, event_id, at) "
            f"SELECT ?, ?, ?, ?, ?, {self.now()} "
            f"WHERE (SELECT COUNT(*) FROM alerts WHERE sink = ? AND sent_at IS NULL) < {ALERT_CAP_PER_SINK}",
            (sink, tenant, kind, reason, event_id, sink),
        )

    def enqueue_alert(self, sink: str, tenant: str | None, kind: str, reason: str, event_id: str | None = None) -> None:
        with self.tx() as t:
            self._insert_alert(t, sink, tenant, kind, reason, event_id)

    def alert_sinks_waiting(self) -> list[str]:
        with self.tx(write=False) as t:
            return [r["sink"] for r in self._rows(t.execute("SELECT DISTINCT sink FROM alerts WHERE sent_at IS NULL ORDER BY sink"))]

    def alert_gate(self, sink: str, interval_s: float) -> bool:
        """True for exactly one caller per interval per sink."""
        with self.tx() as t:
            t.execute("INSERT INTO alert_state (sink) VALUES (?) ON CONFLICT (sink) DO NOTHING", (sink,))
            cur = t.execute(
                f"UPDATE alert_state SET last_sent_at = {self.now()} "
                f"WHERE sink = ? AND (last_sent_at IS NULL OR last_sent_at <= {self.now(-interval_s)})",
                (sink,),
            )
            return cur.rowcount == 1

    def alert_gate_release(self, sink: str, interval_s: float, retry_in_s: float) -> None:
        """The digest did not go out: let the gate open again after retry_in_s instead of a full interval."""
        with self.tx() as t:
            t.execute(
                f"UPDATE alert_state SET last_sent_at = {self.now(retry_in_s - interval_s)} WHERE sink = ?",
                (sink,),
            )

    def alerts_unsent(self, sink: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.tx(write=False) as t:
            return self._rows(
                t.execute(
                    "SELECT id, sink, tenant, kind, reason, event_id FROM alerts WHERE sink = ? AND sent_at IS NULL ORDER BY id LIMIT ?",
                    (sink, limit),
                )
            )

    def alerts_mark_sent(self, ids: list[int]) -> None:
        if not ids:
            return
        marks = ", ".join("?" for _ in ids)
        with self.tx() as t:
            t.execute(f"UPDATE alerts SET sent_at = {self.now()} WHERE id IN ({marks})", tuple(ids))

    # ---- reads ---------------------------------------------------------------------------------------------------
    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self.tx(write=False) as t:
            row = self._one(
                t,
                "SELECT event_id, tenant, source, dedupe_basis, payload, received_at FROM events WHERE event_id = ?",
                (event_id,),
            )
        if row is not None:
            row["received_epoch"] = to_epoch(row["received_at"])
        return row

    def delivery(self, event_id: str, sink: str) -> dict[str, Any] | None:
        with self.tx(write=False) as t:
            return self._one(t, "SELECT * FROM deliveries WHERE event_id = ? AND sink = ?", (event_id, sink))

    def deliveries(self, event_id: str | None = None) -> list[dict[str, Any]]:
        with self.tx(write=False) as t:
            if event_id is None:
                return self._rows(t.execute("SELECT * FROM deliveries ORDER BY event_id, sink"))
            return self._rows(t.execute("SELECT * FROM deliveries WHERE event_id = ? ORDER BY sink", (event_id,)))

    def attempts_for(self, event_id: str, sink: str) -> list[dict[str, Any]]:
        with self.tx(write=False) as t:
            return self._rows(
                t.execute("SELECT * FROM attempts WHERE event_id = ? AND sink = ? ORDER BY n", (event_id, sink))
            )

    def events(self) -> list[dict[str, Any]]:
        with self.tx(write=False) as t:
            return self._rows(t.execute("SELECT * FROM events ORDER BY received_at, event_id"))

    def sink_state(self, tenant: str, sink: str) -> dict[str, Any] | None:
        n = self.now()
        with self.tx(write=False) as t:
            return self._one(
                t,
                f"SELECT tenant, sink, reason, consecutive_failures, "
                f"(paused_until IS NOT NULL AND paused_until > {n}) AS paused, "
                f"(breaker_until IS NOT NULL AND breaker_until > {n}) AS breaker_open, "
                f"(next_send_at IS NOT NULL AND next_send_at > {n}) AS waiting FROM sink_state WHERE tenant = ? AND sink = ?",
                (tenant, sink),
            )

    def health_counts(self) -> dict[str, Any]:
        with self.tx(write=False) as t:
            sup = t.execute("SELECT COUNT(*) AS n FROM suppressed").fetchone()["n"]
            ev = t.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
            by_state = {
                r["state"]: int(r["n"])
                for r in self._rows(t.execute("SELECT state, COUNT(*) AS n FROM deliveries GROUP BY state"))
            }
        return {"suppressed": int(sup), "events": int(ev), "deliveries": by_state}

    def due_in_s(self, sink: str | None = None) -> float | None:
        """Seconds until the earliest pending row (of one sink, or any) becomes claimable, 0 if one is claimable now,
        None if none is pending."""
        g = self.greatest
        parts = [
            self.epoch("d.next_at"),
            f"COALESCE({self.epoch('d.claim_until')}, 0)",
            f"COALESCE({self.epoch('s.paused_until')}, 0)",
            f"COALESCE({self.epoch('s.breaker_until')}, 0)",
            f"COALESCE({self.epoch('s.next_send_at')}, 0)",
        ]
        scope = "" if sink is None else " AND d.sink = ?"
        with self.tx(write=False) as t:
            row = self._one(
                t,
                f"SELECT MIN({g}({', '.join(parts)})) AS due, {self.epoch(self.now())} AS now_e "
                f"FROM deliveries d LEFT JOIN sink_state s ON s.tenant = d.tenant AND s.sink = d.sink "
                f"WHERE d.state = 'pending'{scope}",
                () if sink is None else (sink,),
            )
        if row is None or row["due"] is None:
            return None
        return max(0.0, float(row["due"]) - float(row["now_e"]))

    def pending_sinks(self) -> list[str]:
        with self.tx(write=False) as t:
            return [r["sink"] for r in self._rows(t.execute("SELECT DISTINCT sink FROM deliveries WHERE state = 'pending'"))]

    def dump_all_text(self) -> list[str]:
        """Every value of every column of every table as text. For the secrets-never-stored test."""
        out: list[str] = []
        with self.tx(write=False) as t:
            for table in self._table_names(t):
                for row in self._rows(t.execute(f"SELECT * FROM {table}")):
                    out.extend("" if v is None else str(v) for v in row.values())
        return out

    def _table_names(self, t: Any) -> list[str]:  # pragma: no cover - interface
        raise NotImplementedError
