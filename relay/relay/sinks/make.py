"""Make custom webhook sink. Contract: contracts/make/SOURCE.md.

A 2xx from Make is a QUEUE RECEIPT: the scenario has not necessarily run, and the default `Accepted` body can be
replaced by a Webhook response module, so any 2xx is DONE and the effect reference says "queue". Make documents no
idempotency header; `Idempotency-Key` is the relay's own, for a scenario that wants to deduplicate on it (not tested
here, the README says so). Delivery is at least once.
"""

from __future__ import annotations

import json

from ..http import HttpConnectError, HttpFailure, HttpTimeout
from .base import Event, HttpLike, Outcome, Sink, SinkContext

# doc: apps.make.com/gateway says the maximum webhook payload (Content-Length) is 5 MB, 5,242,880 bytes. The separate
# technical limits page was a 404 in the evidence, so this figure rests on the gateway page alone.
MAX_BODY_BYTES = 5 * 1024 * 1024


class MakeSink(Sink):
    name = "make"
    default_timeout_s = 10.0
    default_min_interval_s = 0.1  # 300 requests per 10 seconds is the documented ceiling, 10 per second is well under it

    def send(self, event: Event, http: HttpLike, ctx: SinkContext) -> Outcome:
        body = json.dumps(
            {
                "event_id": event.event_id,
                "tenant": event.tenant,
                "source": event.source,
                "event_type": event.lead.get("event_type"),
                "lead": event.lead,
                "payload": event.payload,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        if len(body) > MAX_BODY_BYTES:
            return Outcome.dead("payload over 5 MB")
        headers = {"Content-Type": "application/json", "Idempotency-Key": event.event_id}
        if ctx.apikey:
            headers["x-make-apikey"] = ctx.apikey
        try:
            res = http.request("POST", ctx.url, headers=headers, body=body, timeout=ctx.timeout_s)
        except HttpConnectError as exc:
            return Outcome.retry(reason=exc.kind, breaker_failure=True)
        except (HttpTimeout, HttpFailure) as exc:
            return Outcome.ambiguous(reason=exc.kind)

        status = res.status
        if 200 <= status < 300:
            return Outcome.done(status=status, effect_ref="queue")
        if status == 400 and res.text.strip() == "Queue is full":
            return Outcome.retry(status=status, reason="queue is full")
        if status == 429:
            return Outcome.retry(status=status, reason="rate limited", rate_limited=True)
        if status == 410:
            return Outcome.pause("hook inactive (410)", status=status)
        if status == 403:
            return Outcome.pause("forbidden (403)", status=status)
        if status >= 500:
            return Outcome.ambiguous(status=status, reason=f"http {status}")
        return Outcome.dead(f"http {status}", status=status)
