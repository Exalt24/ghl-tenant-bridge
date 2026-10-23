"""What the three pulls share: their errors, the environment they run in (an injected clock, sleeper and random source, so no
test ever waits), the backoff, and a limiter for a vendor that states a calls-per-window ceiling.

Three outcomes besides success, and each one has its own exit code in `python -m relay.pulls`:
  PullThrottled  the vendor asked for a wait longer than this run will hold. Nothing was written. The next scheduled run
                 pulls the same window again, which is safe because a pull is an upsert on its primary key.
  PullAbort      the vendor refused our credentials. Stop, alert, write nothing. Retrying a refused key only gets it flagged.
  PullError      anything else that makes the data untrustworthy (a malformed row, an unknown currency, a repeated cursor).

A pull is ALL OR NOTHING: rows are gathered in memory and written in one transaction at the end, so a run that dies on page
nine leaves the table as the last good run left it, never half restated.

No message built here carries a URL, a header or a response body: the URL holds the access token or API key, and a vendor's
error page can echo it back.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable

__all__ = ["SLEEP_PAD_S", "PullAbort", "PullEnv", "PullError", "PullResult", "PullThrottled", "WindowLimiter", "backoff_s"]

MAX_TRIES = 6
MAX_WAIT_S = 300.0  # one wait inside a run; a vendor that asks for longer is answered by ending the run
MAX_TOTAL_WAIT_S = 900.0  # all waits in one run
EXPONENT_CAP = 30   # an uncapped exponent past 1023 raises OverflowError when multiplied by a float
BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 120.0
MAX_PAGES = 2000  # a corrupt cursor chain must end somewhere
SLEEP_PAD_S = 0.001  # a limiter sleeps this much past the moment a slot frees, so float rounding can never leave it one ulp short and spinning


class PullError(Exception):
    """A readable reason. Never a URL, a key or a response body."""


class PullAbort(PullError):
    """Credentials refused: stop and alert."""


class PullThrottled(PullError):
    def __init__(self, after_s: float, reason: str = "throttled") -> None:
        super().__init__(reason)
        self.after_s = after_s


@dataclass
class PullResult:
    rows: int = 0
    requests: int = 0
    pages: int = 0
    splits: int = 0
    waited_s: float = 0.0
    skipped: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


def _default_sleeper(seconds: float) -> None:
    threading.Event().wait(seconds)


@dataclass
class PullEnv:
    http: Any
    sleeper: Callable[[float], None] = _default_sleeper
    rng: random.Random = field(default_factory=random.Random)
    monotonic: Callable[[], float] = time.monotonic
    today: Callable[[], date] = lambda: datetime.now(timezone.utc).date()


def backoff_s(attempt: int, rng: random.Random, base: float = BACKOFF_BASE_S, cap: float = BACKOFF_CAP_S) -> float:
    """min(base * 2^attempt, cap) with jitter between half and all of it, so concurrent pulls do not retry in step."""
    top = min(base * (2 ** min(max(attempt, 0), EXPONENT_CAP)), cap)   # capped: 2 ** 1024 no longer converts to a float
    return top / 2 + rng.random() * top / 2


class Waiter:
    """Spends the run's wait budget. A wait over MAX_WAIT_S, or one that would pass MAX_TOTAL_WAIT_S, ends the run."""

    def __init__(self, env: PullEnv, result: PullResult) -> None:
        self.env = env
        self.result = result

    def wait(self, seconds: float, reason: str) -> None:
        if seconds > MAX_WAIT_S or self.result.waited_s + seconds > MAX_TOTAL_WAIT_S:
            raise PullThrottled(seconds, reason)
        self.result.waited_s += seconds
        self.env.sleeper(seconds)


class WindowLimiter:
    """At most `limit` calls in any `window_s` seconds, measured on an injected clock. `acquire` waits through the injected
    sleeper until a slot is free and records the call. It never sleeps when a slot is free."""

    def __init__(self, limit: int, window_s: float, clock: Callable[[], float], sleeper: Callable[[float], None]) -> None:
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        self.sleeper = sleeper
        self.calls: deque[float] = deque()

    def ready_in(self) -> float:
        now = self.clock()
        while self.calls and now - self.calls[0] >= self.window_s:
            self.calls.popleft()
        if len(self.calls) < self.limit:
            return 0.0
        return self.window_s - (now - self.calls[0])

    def record(self) -> None:
        self.calls.append(self.clock())

    def acquire(self) -> float:
        waited = 0.0
        while True:
            need = self.ready_in()
            if need <= 0:
                self.record()
                return waited
            self.sleeper(need + SLEEP_PAD_S)
            waited += need + SLEEP_PAD_S
