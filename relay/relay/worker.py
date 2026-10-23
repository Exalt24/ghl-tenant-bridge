"""The worker: drains due deliveries, one claimed row at a time.

`python -m relay.worker`            loops until stopped
`python -m relay.worker --once`     drains what is due and exits (a Railway cron service must be a separate one-shot
                                    service, and needs Postgres)
`python -m relay.worker --purge`    nulls event payloads older than 30 days and exits

Each sink runs in its own thread with no barrier between sinks, so a vendor that hangs holds only its own thread
(bounded by its mandatory timeout) and never delays another sink. In loop mode a sink takes at most `per_sink_cap`
rows per burst. Per sink minimum intervals are not sleeps: they are a `next_send_at` in the database that makes a row
unclaimable until it is due. The only waiting here is the injected `sleeper`, used to idle between bursts and to wait
for a row that is due within a few seconds.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping

from . import alerts, redact
from .config import Config, ConfigError, load_config
from .fanout import Hooks, RunContext, process_next
from .http import Http
from .sinks.registry import build_registry
from .store import open_store
from .store.base import SqlStore

log = logging.getLogger("relay.worker")

PER_SINK_CAP = 5
WAIT_HORIZON_S = 5.0
MAX_TOTAL_WAIT_S = 120.0
MIN_WAIT_S = 0.05
PURGE_AFTER_DAYS = 30.0


class Worker:
    def __init__(
        self,
        cfg: Config,
        store: SqlStore,
        http: Any,
        *,
        rng: random.Random | None = None,
        sleeper: Callable[[float], None] | None = None,
        hooks: Hooks | None = None,
        registry: dict | None = None,
        per_sink_cap: int = PER_SINK_CAP,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.http = http
        self.stop = threading.Event()
        self.sleeper = sleeper or self.stop.wait
        self.per_sink_cap = per_sink_cap
        self.ctx = RunContext(
            store=store, cfg=cfg, registry=registry if registry is not None else build_registry(),
            http=http, rng=rng or random.Random(), hooks=hooks or Hooks(),
        )

    def _drain_sink(
        self, sink: str, wait_horizon_s: float, max_total_wait_s: float, cap: int | None = None
    ) -> int:
        """Run one sink's rows, one claimed row at a time, until none is due. Each sink has its own thread, so a vendor
        that hangs (bounded by its mandatory timeout) delays nobody else."""
        done = 0
        waited = 0.0
        try:
            while not self.stop.is_set() and (cap is None or done < cap):
                if process_next(self.ctx, sink):
                    done += 1
                    continue
                due = self.store.due_in_s(sink)
                if due is None or due > wait_horizon_s:
                    break
                wait = max(due, MIN_WAIT_S)
                if waited + wait > max_total_wait_s:
                    break
                waited += wait
                self.sleeper(wait)
        except Exception as exc:  # a database error on one sink must not stop the others
            log.error("worker for sink %s failed: %s", sink, type(exc).__name__)
        return done

    def _housekeeping(self) -> None:
        try:
            self.store.beat("worker")  # one row with the time, on every cycle; relay.health_check reads it
        except Exception as exc:  # a failed beat shows up as a stale heartbeat, it must not stop the rest of the cycle
            log.error("heartbeat write failed: %s", type(exc).__name__)
        ages: dict[str, float] = {}
        for tcfg in self.cfg.tenants.values():
            for name, scfg in tcfg.sinks.items():
                ages[name] = max(ages.get(name, 0.0), scfg.max_age_s)
        if ages:
            self.store.expire_old(ages)
        alerts.flush(self.store, self.http, self.cfg)

    def run_once(self, wait_horizon_s: float = WAIT_HORIZON_S, max_total_wait_s: float = MAX_TOTAL_WAIT_S) -> int:
        """Drain every due delivery. Rows that become due within the horizon (a minimum interval, a short backoff) are
        waited for through the injected sleeper, up to a total wait budget per sink; anything later, or past the
        budget, is left for the next run. The budget is what stops a vendor that keeps answering 429 with a one second
        Retry-After from holding a one-shot run open for ever."""
        sinks = sorted(self.ctx.registry)
        with ThreadPoolExecutor(max_workers=len(sinks)) as pool:
            total = sum(pool.map(lambda s: self._drain_sink_and_release(s, wait_horizon_s, max_total_wait_s), sinks))
        self._housekeeping()
        return total

    def _drain_sink_and_release(self, sink: str, wait_horizon_s: float, max_total_wait_s: float) -> int:
        """One pass thread: drain, then close the database connection that thread opened, so repeated passes do not pile up
        connections (the loop mode keeps its threads, and their connections, for the life of the process)."""
        try:
            return self._drain_sink(sink, wait_horizon_s, max_total_wait_s)
        finally:
            self.store.release_connection()

    def _sink_loop(self, sink: str, interval_s: float) -> None:
        while not self.stop.is_set():
            n = self._drain_sink(sink, 0.0, 0.0, cap=self.per_sink_cap)
            if n < self.per_sink_cap:
                self.sleeper(interval_s)

    def loop(self, interval_s: float = 5.0) -> None:
        """One thread per sink, each taking at most per_sink_cap rows per burst, plus housekeeping here."""
        threads = [
            threading.Thread(target=self._sink_loop, args=(s, interval_s), name=f"relay-{s}", daemon=True)
            for s in sorted(self.ctx.registry)
        ]
        for t in threads:
            t.start()
        while not self.stop.is_set():
            try:
                self._housekeeping()
            except Exception as exc:
                log.error("housekeeping failed: %s", type(exc).__name__)
            self.sleeper(interval_s)
        for t in threads:
            t.join(timeout=interval_s + 15)

    def purge(self) -> int:
        n = self.store.purge(PURGE_AFTER_DAYS)
        log.info("purged %d payloads older than %d days", n, int(PURGE_AFTER_DAYS))
        return n


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None, http: Any = None, store: SqlStore | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay.worker")
    parser.add_argument("--once", action="store_true", help="drain what is due, then exit")
    parser.add_argument("--purge", action="store_true", help="null payloads older than 30 days, then exit")
    parser.add_argument("--interval", type=float, default=None, help="seconds between passes when looping")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    role = "worker_once" if (args.once or args.purge) else "worker"
    try:
        cfg = load_config(environ, role=role)
    except ConfigError as exc:
        print(f"relay.worker: configuration refused: {exc}", file=sys.stderr)
        return 2
    redact.install()
    store = store or open_store(cfg.database_url, cfg.db_path)
    worker = Worker(cfg, store, http or Http())
    if args.purge:
        worker.purge()
        return 0
    if args.once:
        worker.run_once()
        return 0
    worker.loop(args.interval if args.interval is not None else cfg.worker_interval_s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
