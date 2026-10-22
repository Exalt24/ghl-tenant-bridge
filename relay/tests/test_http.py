"""The HTTP seam: which transport failure becomes which class, and that no URL rides along."""

from __future__ import annotations

import httpx
import pytest

from relay.http import Http, HttpConnectError, HttpFailure, HttpTimeout

URL = "https://hooks.example.test/services/SECRETPART/ANOTHERSECRET"


def client_raising(exc: Exception) -> Http:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return Http(httpx.Client(transport=httpx.MockTransport(handler)))


def test_a_normal_response_is_returned_with_lowercase_headers_and_trimmed_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 5000, headers={"Retry-After": "7"})

    res = Http(httpx.Client(transport=httpx.MockTransport(handler))).request("POST", URL, body=b"{}", timeout=1.0)
    assert res.status == 200 and res.headers["retry-after"] == "7" and len(res.text) == 2000


@pytest.mark.parametrize(
    "exc,expected",
    [
        (httpx.ConnectTimeout("connect timed out"), HttpConnectError),  # nothing was sent
        (httpx.PoolTimeout("no connection free"), HttpConnectError),  # nothing was sent
        (httpx.ConnectError("refused"), HttpConnectError),
        (httpx.ReadTimeout("read timed out"), HttpTimeout),  # the server may have processed it
        (httpx.WriteTimeout("write timed out"), HttpTimeout),
        (httpx.RemoteProtocolError("peer closed"), HttpFailure),
        (httpx.ReadError("reset"), HttpFailure),
    ],
)
def test_each_transport_failure_maps_to_one_class_and_carries_only_the_httpx_class_name(exc, expected):
    h = client_raising(exc)
    # positive control: the same client class does work when the handler does not raise
    ok = Http(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(204))))
    assert ok.request("GET", URL, timeout=1.0).status == 204
    with pytest.raises(expected) as err:
        h.request("POST", URL, body=b"{}", timeout=1.0)
    assert err.value.kind == type(exc).__name__
    assert "SECRETPART" not in str(err.value) and err.value.__cause__ is None and err.value.__suppress_context__


def test_a_caller_can_ask_for_more_of_the_reply_than_a_sink_keeps():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="y" * 5000)

    h = Http(httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(h.request("GET", URL, timeout=1.0).text) == 2000  # control: the default cut
    assert len(h.request("GET", URL, timeout=1.0, max_text=100000).text) == 5000
