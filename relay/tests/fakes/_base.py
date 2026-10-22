"""Shared machinery for the vendor fakes: a real HTTP server on port 0, a script of per-request modes, a record of what
arrived, and a count of EFFECTS (requests the fake would have acted on). The effect count is what the crash and
duplicate tests assert on, because "the vendor acted" is the thing that must not repeat.

A fake is a hand written reading of the vendor's documentation. Where it is stricter than the vendor on purpose, the
rule has a name in `self.rules` so a test (and a mutant) can switch it off.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from typing import Any

from werkzeug.serving import make_server
from werkzeug.wrappers import Request, Response

HANG_CAP_S = 20.0  # a hung request is released by the test; this is only the backstop so a bug cannot hang the run


class FakeVendor:
    rules: set[str]
    default_mode = "ok"

    def __init__(self) -> None:
        self.script: deque[str] = deque()
        self.requests: list[dict[str, Any]] = []
        self.effects = 0
        self.release = threading.Event()
        self.lock = threading.Lock()
        self._server = None
        self._thread: threading.Thread | None = None
        self.mode = self.default_mode

    # ---- lifecycle -----------------------------------------------------------------------------------------------
    def start(self) -> "FakeVendor":
        self._server = make_server("127.0.0.1", 0, self, threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def base(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_port}"

    def stop(self) -> None:
        self.release.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def reset(self) -> None:
        with self.lock:
            self.script.clear()
            self.requests.clear()
            self.effects = 0
            self.mode = self.default_mode
        self.release.clear()

    # ---- scripting -----------------------------------------------------------------------------------------------
    def next_mode(self) -> str:
        with self.lock:
            return self.script.popleft() if self.script else self.mode

    def queue(self, *modes: str) -> None:
        with self.lock:
            self.script.extend(modes)

    def record(self, request: Request, parsed: Any, mode: str, accepted: bool) -> None:
        with self.lock:
            self.requests.append(
                {
                    "method": request.method,
                    "path": request.path,
                    "headers": {k.lower(): v for k, v in request.headers.items()},
                    "json": parsed,
                    "mode": mode,
                    "accepted": accepted,
                }
            )
            if accepted:
                self.effects += 1

    def hang(self) -> None:
        self.release.wait(HANG_CAP_S)

    # ---- WSGI ----------------------------------------------------------------------------------------------------
    def handle(self, request: Request) -> Response:  # pragma: no cover - interface
        raise NotImplementedError

    def __call__(self, environ, start_response):  # type: ignore[no-untyped-def]
        request = Request(environ)
        response = self.handle(request)
        return response(environ, start_response)

    @staticmethod
    def parse_json(request: Request) -> Any:
        raw = request.get_data()
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
