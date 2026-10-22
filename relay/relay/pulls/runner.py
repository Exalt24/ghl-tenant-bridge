"""Run one pull and turn how it ended into an exit code and, when a person has to act, an alert.

  0  pulled and stored
  1  the data could not be trusted (a malformed row, an unknown currency, a repeated cursor): alert, nothing written
  2  the configuration was refused (raised by the CLI before this module runs)
  3  throttled for longer than the run will wait: nothing written, the next scheduled run pulls the same window again
  4  the vendor refused the credentials: alert, nothing written, and a person has to replace the key or the token

A refused key is never retried in a loop, because repeated attempts with a revoked key are how an account gets flagged.
"""

from __future__ import annotations

import logging

from .. import alerts
from ..config import Config
from ..store.base import SqlStore
from .common import PullAbort, PullEnv, PullError, PullResult, PullThrottled
from .fathom import pull_fathom
from .meta_spend import pull_meta_spend
from .windsor import pull_windsor

__all__ = ["EXIT_ABORTED", "EXIT_CONFIG", "EXIT_ERROR", "EXIT_OK", "EXIT_THROTTLED", "PULLS", "run_pull"]

log = logging.getLogger("relay.pulls")

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_THROTTLED, EXIT_ABORTED = 0, 1, 2, 3, 4
PULLS = ("meta_insights", "windsor", "fathom")


def _alert(store: SqlStore, cfg: Config, env: PullEnv, name: str, tenant: str, kind: str, reason: str) -> None:
    try:
        store.enqueue_alert(name, tenant, kind, reason)
        alerts.flush(store, env.http, cfg)
    except Exception as exc:  # the alert is best effort: the exit code already tells the scheduler the run failed
        log.error("could not queue the pull alert: %s", type(exc).__name__)


def run_pull(name: str, store: SqlStore, cfg: Config, tenant: str, env: PullEnv) -> int:
    pcfg = cfg.tenants[tenant].pulls[name]
    try:
        result: PullResult
        if name == "meta_insights":
            result = pull_meta_spend(store, pcfg, env)
        elif name == "windsor":
            result = pull_windsor(store, pcfg, env)
        elif name == "fathom":
            result = pull_fathom(store, pcfg, env, tenant=tenant)
        else:
            raise PullError("unknown pull")
    except PullAbort as exc:
        log.error("pull %s for %s stopped, credentials refused: %s", name, tenant, exc)
        _alert(store, cfg, env, name, tenant, "pause", f"credentials refused: {exc}")
        return EXIT_ABORTED
    except PullThrottled as exc:
        log.warning("pull %s for %s throttled, try again in about %d seconds: %s", name, tenant, int(exc.after_s), exc)
        return EXIT_THROTTLED
    except PullError as exc:
        log.error("pull %s for %s failed, nothing written: %s", name, tenant, exc)
        _alert(store, cfg, env, name, tenant, "dead", f"pull failed: {exc}")
        return EXIT_ERROR
    log.info("pull %s for %s stored %d rows from %d requests (%d pages, %d range splits)", name, tenant, result.rows, result.requests, result.pages, result.splits)
    return EXIT_OK

