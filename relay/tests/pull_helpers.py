"""Builders for the pull tests: a PullCfg over a local fake, a PullEnv whose clock and sleeper are simulated (so no test waits),
and a scripted HTTP stub for the cases a fake cannot produce (a repeated cursor, a row that spans two days)."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from relay.config import PullCfg
from relay.http import Http, HttpResult
from relay.pulls.common import PullEnv

FIXED_TODAY = date(2026, 10, 21)


@dataclass
class Clock:
    """A simulated monotonic clock. The sleeper advances it, so a limiter and a backoff can be measured without waiting."""

    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def pull_env(http: Any = None, clock: Clock | None = None, seed: int = 1) -> tuple[PullEnv, Clock]:
    clock = clock or Clock()
    env = PullEnv(http=http if http is not None else Http(), sleeper=clock.sleep, rng=random.Random(seed), monotonic=clock.monotonic, today=lambda: FIXED_TODAY)
    return env, clock


def insights_cfg(fake, *, days: int = 28, limit: int = 500, account: str | None = None, key: str | None = None, timeout_s: float = 5.0) -> PullCfg:
    from .fakes.ads import ACCOUNT, TOKEN

    return PullCfg(name="meta_insights", base_url=fake.url, key=key or TOKEN, account=account or ACCOUNT, days=days, timeout_s=timeout_s, extra={"limit": limit})


def windsor_cfg(fake, *, days: int = 28, label: str = "default", key: str | None = None, timeout_s: float = 5.0) -> PullCfg:
    from .fakes.ads import WINDSOR_KEY

    return PullCfg(name="windsor", base_url=fake.url, key=key or WINDSOR_KEY, account=label, days=days, timeout_s=timeout_s, extra={})


def fathom_cfg(fake, *, days: int = 7, include_summary: bool = False, key: str | None = None, timeout_s: float = 5.0) -> PullCfg:
    from .fakes.fathom import API_KEY

    return PullCfg(name="fathom", base_url=fake.url, key=key or API_KEY, account="default", days=days, timeout_s=timeout_s, extra={"include_summary": include_summary})


def reply(status: int = 200, text: str = "", **headers: str) -> HttpResult:
    return HttpResult(status=status, headers={k.lower().replace("_", "-"): v for k, v in headers.items()}, text=text)


class StubHttp:
    """Answers from a script, in order, and remembers every URL and header set it was asked for. An Exception in the script
    is raised instead of returned."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.urls: list[str] = []
        self.headers: list[dict[str, str] | None] = []

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None, timeout: float, max_text: int = 2000) -> HttpResult:
        self.urls.append(url)
        self.headers.append(headers)
        if not self.script:
            raise AssertionError("the stub ran out of scripted answers")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step
