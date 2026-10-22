"""The one HTTP seam. Sinks take an object with `request(...)`, so tests and the worker inject it.

Failures are re-raised as three small classes carrying only the httpx class name. The original exception is dropped
(`from None`) because httpx messages and chained causes can carry the request URL, and a hook URL is a secret.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

MAX_BODY_TEXT = 2000


class HttpError(Exception):
    """Base of the three. `kind` is the httpx class name and nothing else, so it is safe to store and to log."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class HttpTimeout(HttpError):
    """The request may or may not have been processed (a read or write timed out)."""


class HttpConnectError(HttpError):
    """The connection was never made, so the vendor cannot have acted."""


class HttpFailure(HttpError):
    """Any other transport failure (reset, protocol error). The vendor may have acted."""


@dataclass
class HttpResult:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""


class Http:
    def __init__(self, client: httpx.Client | None = None) -> None:
        # trust_env off: a proxy variable in the environment must not silently reroute a webhook.
        self._client = client or httpx.Client(follow_redirects=False, trust_env=False)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float,
    ) -> HttpResult:
        try:
            res = self._client.request(method, url, headers=headers, content=body, timeout=timeout)
        except (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError) as exc:
            # Never connected (or never got a pooled connection), so nothing was sent: the vendor cannot have acted.
            raise HttpConnectError(type(exc).__name__) from None
        except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            raise HttpTimeout(type(exc).__name__) from None
        except httpx.HTTPError as exc:
            raise HttpFailure(type(exc).__name__) from None
        return HttpResult(
            status=res.status_code,
            headers={k.lower(): v for k, v in res.headers.items()},
            text=res.text[:MAX_BODY_TEXT],
        )

    def close(self) -> None:
        self._client.close()
