"""The HTTP bridge. HighLevel posts an outbound message to the Delivery URL, the text goes out through the phone."""

from __future__ import annotations

import hmac
import json
import logging
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import config as config_mod
from . import signature
from .gateway import Gateway, GatewayRefused, GatewayUnreachable
from .highlevel import HighLevelClient, HighLevelError, NoReporter
from .store import Store

log = logging.getLogger("sms_bridge")

MAX_BODY = 64 * 1024
MAX_TEXT = 1600
PHONE = re.compile(r"^\+[1-9][0-9]{6,14}$")
ROUTE = "/outbound"


def token_ok(expected: str, query_token: str | None, header_token: str | None) -> bool:
    """The secret may arrive as ?token=... or as an X-Bridge-Token header. Compared in constant time."""
    for given in (query_token, header_token):
        if given and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
            return True
    return False


class Rejected(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def validate(raw: bytes) -> dict:
    """Parse and check the outbound webhook body. Raises Rejected with 400 or 422."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Rejected(400, "body is not JSON") from None
    if not isinstance(body, dict):
        raise Rejected(400, "body must be a JSON object")
    message_id = body.get("messageId")
    if not isinstance(message_id, str) or not message_id.strip():
        raise Rejected(400, "messageId is required")
    kind = body.get("type")
    if not isinstance(kind, str) or not kind.strip():
        raise Rejected(400, "type is required")
    if kind != "SMS":
        raise Rejected(422, "type %r is not supported, only SMS" % kind[:20])
    phone = body.get("phone")
    if not isinstance(phone, str) or not PHONE.match(phone.strip()):
        raise Rejected(400, "phone must be an E.164 number such as +15864603685")
    text = body.get("message")
    if not isinstance(text, str) or not text.strip():
        raise Rejected(400, "message is required")
    if len(text) > MAX_TEXT:
        raise Rejected(400, "message is longer than %d characters" % MAX_TEXT)
    attachments = body.get("attachments", [])
    if not isinstance(attachments, list):
        raise Rejected(400, "attachments must be a list")
    return {"messageId": message_id.strip(), "phone": phone.strip(), "message": text, "attachments": len(attachments)}


class Bridge:
    """The logic, with the gateway and the HighLevel client passed in so a test can stand in for either."""

    def __init__(self, cfg, store, gateway, reporter):
        self.cfg = cfg
        self.ghl_key = signature.load_key(cfg.ghl_public_key_b64) if cfg.ghl_signature_mode != "off" else None
        self.store = store
        self.gateway = gateway
        self.reporter = reporter
        self._threads: list[threading.Thread] = []
        self._tlock = threading.Lock()

    def handle(self, msg: dict) -> tuple[int, dict]:
        mid = msg["messageId"]
        claimed, rec = self.store.claim(mid)
        if not claimed:
            return 200, {"ok": True, "duplicate": True, "messageId": mid, "status": rec.get("status")}
        try:
            sent = self.gateway.send(msg["phone"], msg["message"])
        except GatewayRefused as exc:
            self._finish(mid, "failed", "gateway", "The gateway refused the text (%d)" % exc.status, retryable=True)
            return 502, {"ok": False, "messageId": mid, "error": "gateway refused the text (%d)" % exc.status}
        except GatewayUnreachable as exc:
            self._finish(mid, "failed", "gateway", "No answer from the gateway (%s)" % exc)
            return 502, {"ok": False, "messageId": mid, "error": "no answer from the gateway"}
        self.store.update(mid, status="queued", gatewayId=sent["id"], gatewayState=sent["state"])
        t = threading.Thread(target=self._follow, args=(mid, sent["id"]), daemon=True)
        with self._tlock:
            self._threads.append(t)
        t.start()
        return 200, {"ok": True, "messageId": mid, "status": "queued", "gatewayId": sent["id"], "state": sent["state"],
                     "attachmentsIgnored": msg["attachments"]}

    def _follow(self, mid: str, gateway_id: str) -> None:
        result = self.gateway.wait(gateway_id, self.cfg.poll_interval, self.cfg.poll_deadline)
        if result["state"] == "Delivered":
            self._finish(mid, "delivered", gateway_state="Delivered")
        elif result["state"] == "Failed":
            reason = result["error"] or "unknown"
            self._finish(mid, "failed", reason, "The phone could not send the text: %s" % reason, gateway_state="Failed")
        else:
            self._finish(mid, "failed", "timeout",
                         "No final state within %g seconds, last state %s" % (self.cfg.poll_deadline, result["state"]),
                         gateway_state=result["state"])

    def _finish(self, mid, status, code=None, text=None, retryable=False, gateway_state=None) -> None:
        fields = {"status": status, "retryable": retryable}
        if gateway_state:
            fields["gatewayState"] = gateway_state
        if text:
            fields["error"] = text
        self.store.update(mid, **fields)
        error = {"code": code, "type": "sms_gateway", "message": text} if status == "failed" else None
        try:
            self.reporter.update_status(mid, status, error)
            self.store.update(mid, reported=True)
        except HighLevelError as exc:
            log.warning("status callback for %s not accepted: %s", mid, exc)
            self.store.update(mid, reported=False, reportError=str(exc))

    def join(self, timeout: float = 5.0) -> None:
        with self._tlock:
            threads = list(self._threads)
        for t in threads:
            t.join(timeout)


def make_handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "sms-bridge"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # the request line carries ?token=, so it is never logged
            log.info("%s %s", self.command, urlsplit(self.path).path)

        body_read = False

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if not self.body_read:  # an unread body would be taken for the next request on a kept-alive connection
                self.send_header("Connection", "close")
                self.close_connection = True
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.body_read = True
            if urlsplit(self.path).path == "/health":
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            self.body_read = False
            parts = urlsplit(self.path)
            if parts.path != ROUTE:
                return self._send(404, {"error": "not found"})
            query_token = (parse_qs(parts.query).get("token") or [None])[0]
            if not token_ok(bridge.cfg.bridge_token, query_token, self.headers.get("X-Bridge-Token")):
                return self._send(401, {"error": "bad or missing token"})
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._send(400, {"error": "Content-Length is required"})
            if length < 0:
                return self._send(400, {"error": "Content-Length is required"})
            if length > MAX_BODY:
                self.rfile.read(min(length, 1 << 20))  # let the sender finish, or it sees a reset instead of the 413
                return self._send(413, {"error": "body is too large"})
            raw = self.rfile.read(length)
            self.body_read = True
            if bridge.cfg.ghl_signature_mode != "off":
                # Over the bytes as received, before anything parses them: nothing below runs on an unproven body in enforce mode.
                verdict = signature.check(self.headers, raw, bridge.ghl_key)
                if verdict != signature.OK:
                    if bridge.cfg.ghl_signature_mode == "enforce":
                        return self._send(401, {"error": "bad or missing signature"})
                    log.warning("signature check failed (%s); accepted because GHL_SIGNATURE_MODE is warn", verdict)
            try:
                msg = validate(raw)
            except Rejected as exc:
                return self._send(exc.status, {"error": exc.reason})
            status, payload = bridge.handle(msg)
            self._send(status, payload)

    return Handler


def build(cfg, gateway=None, reporter=None) -> Bridge:
    gateway = gateway or Gateway(cfg.gateway_url, cfg.gateway_user, cfg.gateway_password, cfg.sim_number)
    if reporter is None:
        reporter = HighLevelClient(cfg.highlevel_token, cfg.highlevel_base, cfg.highlevel_version) if cfg.highlevel_token else NoReporter()
    return Bridge(cfg, Store(cfg.store_path), gateway, reporter)


def serve(bridge: Bridge, host=None, port=None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host or bridge.cfg.host, bridge.cfg.port if port is None else port), make_handler(bridge))
    server.daemon_threads = True
    return server


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        cfg = config_mod.from_env()
    except config_mod.ConfigError as exc:
        print("sms-bridge: " + str(exc), file=sys.stderr)
        return 2
    bridge = build(cfg)
    if not cfg.highlevel_token:
        log.warning("HIGHLEVEL_ACCESS_TOKEN is not set: texts go out, but HighLevel is not told the result")
    server = serve(bridge)
    log.info("listening on %s:%d, POST %s?token=...", cfg.host, server.server_address[1], ROUTE)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
