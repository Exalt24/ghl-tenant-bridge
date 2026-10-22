"""`python -m relay.pulls meta_insights|windsor|fathom --tenant <name>`: one pull, then exit with a code a scheduler can read.

Meant for a cron style service, which needs Postgres (a separate service cannot share a SQLite file). See runner.py for the
exit codes. `--days N` overrides the window the config names for this run.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from typing import Any, Mapping

from .. import redact
from ..config import ConfigError, load_config
from ..http import Http
from ..store import open_store
from ..store.base import SqlStore
from .common import PullEnv
from .runner import EXIT_CONFIG, PULLS, run_pull


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None, http: Any = None, store: SqlStore | None = None, env: PullEnv | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay.pulls")
    parser.add_argument("pull", choices=PULLS)
    parser.add_argument("--tenant", default=None, help="the tenant whose pull to run (optional when exactly one tenant configures it)")
    parser.add_argument("--days", type=int, default=None, help="pull this many days back for this run")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = load_config(environ, role="worker_once")
    except ConfigError as exc:
        print(f"relay.pulls: configuration refused: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    owners = sorted(t for t, tc in cfg.tenants.items() if args.pull in tc.pulls)
    tenant = args.tenant or (owners[0] if len(owners) == 1 else None)
    if tenant is None or tenant not in owners:
        print(f"relay.pulls: no tenant to run {args.pull} for (configured: {', '.join(owners) or 'none'})", file=sys.stderr)
        return EXIT_CONFIG
    if args.days is not None:
        if not 1 <= args.days <= 365:
            print("relay.pulls: --days must be from 1 to 365", file=sys.stderr)
            return EXIT_CONFIG
        cfg.tenants[tenant].pulls[args.pull] = replace(cfg.tenants[tenant].pulls[args.pull], days=args.days)
    redact.install()
    store = store or open_store(cfg.database_url, cfg.db_path)
    return run_pull(args.pull, store, cfg, tenant, env or PullEnv(http=http or Http()))


if __name__ == "__main__":
    sys.exit(main())
