"""Stand-in servers written from the vendors' documentation, served over real HTTP on a local port.

FakeGateway follows the capcom6 android-sms-gateway local API as seen on 2026-11-10: POST /message answers 202 with
state Pending, then GET /message/{id} walks Pending, Processed, Sent, Delivered or Failed with recipients[].error.
FakeHighLevel follows HighLevel's "Update message status" page: PUT /conversations/messages/{messageId}/status with a
Bearer token, a Version header and a body of status (delivered, failed, pending, read) and an optional error object
whose code, type and message are all strings.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Server:
    def __init__(self):
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _go(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                with outer.lock:
                    status, payload = outer.route(self.command, self.path, dict(self.headers), raw)
                if payload == "HANG":
                    return
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = _go

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeGateway(_Server):
    def __init__(self, user="gwuser", password="gwpass-1234"):
        self.user, self.password = user, password
        self.script = ["Pending", "Processed", "Sent", "Delivered"]
        self.recipient_error = None
        self.post_status = 202
        self.hang_post = False
        self.posts: list[dict] = []
        self.gets = 0
        self.seq = 0
        self.polls: dict[str, int] = {}
        super().__init__()

    def _authed(self, headers):
        want = "Basic " + base64.b64encode(("%s:%s" % (self.user, self.password)).encode()).decode()
        return headers.get("Authorization") == want

    def route(self, method, path, headers, raw):
        if not self._authed(headers):
            return 401, {"message": "Unauthorized"}
        if method == "POST" and path == "/message":
            if self.hang_post:
                return 200, "HANG"
            body = json.loads(raw)
            assert isinstance(body["message"], str) and isinstance(body["phoneNumbers"], list)
            self.posts.append(body)
            if self.post_status != 202:
                return self.post_status, {"message": "refused"}
            self.seq += 1
            gid = "gw%d" % self.seq
            self.polls[gid] = 0
            return 202, {"id": gid, "state": "Pending", "recipients": [{"phoneNumber": body["phoneNumbers"][0], "state": "Pending"}]}
        m = re.fullmatch(r"/message/(\w+)", path)
        if method == "GET" and m and m.group(1) in self.polls:
            gid = m.group(1)
            self.gets += 1
            i = min(self.polls[gid], len(self.script) - 1)
            self.polls[gid] += 1
            state = self.script[i]
            rec = {"phoneNumber": "+15550001111", "state": state}
            if state == "Failed" and self.recipient_error:
                rec["error"] = self.recipient_error
            return 200, {"id": gid, "state": state, "recipients": [rec]}
        return 404, {"message": "not found"}


class FakeHighLevel(_Server):
    def __init__(self, token="hl-token-abcdef", version="2021-04-15"):
        self.token, self.version = token, version
        self.calls: list[dict] = []
        self.answer = 200
        super().__init__()

    def route(self, method, path, headers, raw):
        m = re.fullmatch(r"/conversations/messages/([^/]+)/status", path)
        if not m or method != "PUT":
            return 404, {"message": "not found"}
        if headers.get("Authorization") != "Bearer " + self.token:
            return 401, {"message": "Invalid token"}
        if headers.get("Version") != self.version:
            return 400, {"message": "bad Version"}
        body = json.loads(raw)
        if body.get("status") not in ("delivered", "failed", "pending", "read"):
            return 422, {"message": "bad status"}
        err = body.get("error")
        if err is not None and not (isinstance(err, dict) and all(isinstance(err.get(k), str) for k in ("code", "type", "message"))):
            return 422, {"message": "bad error object"}
        self.calls.append({"messageId": m.group(1), "body": body})
        if self.answer != 200:
            return self.answer, {"message": "no"}
        return 200, {"conversationId": "conv1", "messageId": m.group(1)}
