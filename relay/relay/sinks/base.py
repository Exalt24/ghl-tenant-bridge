"""The contract every sink implements, and the outcome vocabulary the fan-out understands.

Outcome classes
  DONE       the vendor accepted it. Terminal.
  RETRY      the vendor certainly did NOT act (rate limit, queue full, connection refused). Spends no attempt budget.
  AMBIGUOUS  the vendor MAY have acted (5xx, read timeout, reset). Retried, and every try spends the attempt budget
             because every try can create a duplicate.
  DEAD       will never succeed as sent. Terminal and visible, replayable by an operator.
  SKIPPED    deliberately not sent (an event that is not a lead for this sink). Terminal with a visible reason.
  PAUSE      a tenant level fault (revoked hook, forbidden). The lead stays pending and the sink pauses for that tenant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

DONE = "DONE"
RETRY = "RETRY"
AMBIGUOUS = "AMBIGUOUS"
DEAD = "DEAD"
SKIPPED = "SKIPPED"
PAUSE = "PAUSE"

KINDS = (DONE, RETRY, AMBIGUOUS, DEAD, SKIPPED, PAUSE)


@dataclass
class Outcome:
    kind: str
    status: int | None = None
    reason: str | None = None  # readable, never a URL, a token or str(exception)
    after_s: float | None = None  # how long to wait before the next try; None means the default backoff
    effect_ref: str | None = None
    pause_s: float | None = None
    # True when the vendor MAY have acted on this try (an ambiguous send, or a read-back that found nothing yet because
    # the vendor can land a request late). A sink that can read back uses the stored mark to look before it sends again.
    maybe_acted: bool = False
    # A vendor wide rate limit: the whole channel waits, not only this row.
    rate_limited: bool = False
    # Counts toward the circuit breaker (server errors, timeouts, refused connections). A 429 does not.
    breaker_failure: bool = False

    @staticmethod
    def done(status: int | None = 200, effect_ref: str | None = None) -> "Outcome":
        return Outcome(DONE, status=status, effect_ref=effect_ref)

    @staticmethod
    def retry(
        after_s: float | None = None,
        status: int | None = None,
        reason: str | None = None,
        rate_limited: bool = False,
        breaker_failure: bool = False,
    ) -> "Outcome":
        return Outcome(
            RETRY, status=status, reason=reason, after_s=after_s, rate_limited=rate_limited, breaker_failure=breaker_failure
        )

    @staticmethod
    def ambiguous(status: int | None = None, reason: str | None = None) -> "Outcome":
        return Outcome(AMBIGUOUS, status=status, reason=reason, breaker_failure=True, maybe_acted=True)

    @staticmethod
    def dead(reason: str, status: int | None = None) -> "Outcome":
        return Outcome(DEAD, status=status, reason=reason)

    @staticmethod
    def skipped(reason: str) -> "Outcome":
        return Outcome(SKIPPED, reason=reason)

    @staticmethod
    def pause(reason: str, pause_s: float | None = None, status: int | None = None) -> "Outcome":
        return Outcome(PAUSE, status=status, reason=reason, pause_s=pause_s)


@dataclass
class Event:
    event_id: str
    tenant: str
    source: str
    payload: dict[str, Any]
    lead: dict[str, Any]
    # When the relay stored the event, seconds since the Unix epoch. The fallback for an event time and an fbc time.
    received_epoch: float | None = None


@dataclass
class SinkContext:
    """What a sink may know about this try. Secrets arrive here already resolved from the environment."""

    tenant: str
    url: str
    timeout_s: float
    apikey: str | None = None
    attempts: int = 0
    retries: int = 0
    # Per sink settings from the config: literal options plus the values of the environment variables it named.
    extra: dict[str, Any] = field(default_factory=dict)
    # A try before this one may have acted (an ambiguous send, a read-back that found nothing), and how long ago.
    maybe_acted_before: bool = False
    since_maybe_acted_s: float | None = None
    # An earlier claim on this row ended without a final write (the worker died): that try may have acted too.
    prior_claim_lost: bool = False
    # One append in flight per key across processes: `with ctx.lock(key, ttl_s) as got:`. None in unit tests of a sink.
    lock: Any = None


class HttpLike(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str] | None = ..., body: bytes | None = ..., timeout: float) -> Any: ...


class Sink:
    """Subclass, set the class attributes, implement send. One module per sink, one line in the registry."""

    name = "base"
    default_timeout_s = 10.0
    default_min_interval_s = 1.0
    default_pause_s = 900.0
    default_max_age_s = 72 * 3600.0
    # True when reconcile can really look the effect up (a read-back). Slack and Make have no read path.
    can_reconcile = False
    # What the config must supply for this sink: names under the sink's `env` map, whether `apikey_env` is required.
    required_env: tuple[str, ...] = ()
    needs_apikey = False

    @classmethod
    def check_options(cls, options: dict[str, Any], env_values: dict[str, str], live: bool) -> dict[str, Any]:
        """Validate the literal `options` of this sink and return them with defaults filled in. Raise ValueError with a
        readable reason that never contains a secret. The base sink takes none."""
        if options:
            raise ValueError("this sink takes no options")
        return {}

    @classmethod
    def secret_values(cls, env_values: dict[str, str]) -> list[str]:
        """Every value that must be masked in logs. By default every environment value the sink was given."""
        return list(env_values.values())

    def send(self, event: Event, http: HttpLike, ctx: SinkContext) -> Outcome:  # pragma: no cover - interface
        raise NotImplementedError

    def reconcile(self, event: Event, http: HttpLike, ctx: SinkContext) -> str:
        """found | absent | unknown"""
        return "unknown"
