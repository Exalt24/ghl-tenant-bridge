# LIFESPAN: permanent (relay tooling that the README lists as a command)
"""Boots the relay image and sends one lead through it, against a fake Slack running inside the same container.

    python tools/container_smoke.py --build        build the image first, then run the checks
    python tools/container_smoke.py                use an image that is already built (CI builds it in the step before)

What it proves, each with a message when it fails:
  1. the container starts as a non root user and answers GET /health
  2. POST /ingest takes a lead, stores it, and the inline worker inside the container delivers it to the fake Slack once
  3. the same delivery again is a duplicate and posts nothing more

It does NOT prove a deploy: nothing here is Railway, and the vendor on the other end is a fake. Standard library only, so it
runs on a bare CI runner. Waiting is polling in 300 ms steps up to a deadline, never a fixed sleep.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

RELAY = Path(__file__).resolve().parent.parent
IMAGE = "relay-ci"
FAKE_PORT = 8081
HOOK_PATH = "/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"
INGEST_TOKEN = "smoke-ingest-token-0123456789"
LOCATION = "LOCSMOKE"
LEAD_NAME = "Smoke Test Lead"
POLL_S = 0.3


def build_config() -> str:
    return json.dumps({"locations": {LOCATION: "smoke"}, "tenants": {"smoke": {"sinks": {"slack": {"url_env": "SMOKE_SLACK_URL"}}}}})


def lead_payload() -> dict[str, Any]:
    return {
        "type": "ContactCreate", "locationId": LOCATION, "id": "contactSmoke1", "name": LEAD_NAME, "firstName": "Smoke", "lastName": "Lead",
        "email": "smoke@example.com", "phone": "+14155550100", "dateAdded": "2026-10-20T12:41:02.193Z", "tags": ["smoke"],
    }


def docker_run_args(name: str, port: int, image: str = IMAGE, relay_dir: Path = RELAY) -> list[str]:
    env = {
        "RELAY_ENV": "dev", "RELAY_INGEST_TOKEN": INGEST_TOKEN, "RELAY_INLINE_WORKER": "1", "RELAY_WORKER_INTERVAL_S": "1",
        "WEB_CONCURRENCY": "1", "RELAY_CONFIG": build_config(), "SMOKE_SLACK_URL": f"http://127.0.0.1:{FAKE_PORT}{HOOK_PATH}",
    }
    args = ["docker", "run", "-d", "--name", name, "-p", f"{port}:8000", "-v", f"{relay_dir}:/smoke:ro"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    return args + [image]


class SmokeFailure(Exception):
    pass


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    res = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if check and res.returncode != 0:
        raise SmokeFailure(f"`{' '.join(args[:3])}` failed: {res.stderr.strip() or res.stdout.strip()}")
    return res


def wait_for(cond, what: str, timeout_s: float = 60.0) -> Any:
    ticker = threading.Event()
    waited = 0.0
    while waited < timeout_s:
        value = cond()
        if value:
            return value
        ticker.wait(POLL_S)
        waited += POLL_S
    raise SmokeFailure(f"timed out after {timeout_s:.0f}s waiting for {what}")


def http(method: str, url: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, json.loads(res.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def health(port: int) -> dict[str, Any] | None:
    status, body = http("GET", f"http://127.0.0.1:{port}/health")
    return body if status == 200 and isinstance(body, dict) and body.get("ok") else None


def post_lead(port: int, key: str) -> tuple[int, Any]:
    return http(
        "POST", f"http://127.0.0.1:{port}/ingest", json.dumps(lead_payload()).encode("utf-8"),
        {"Authorization": f"Bearer {INGEST_TOKEN}", "Idempotency-Key": key, "Content-Type": "application/json"},
    )


def fake_lines(name: str) -> list[dict[str, Any]]:
    res = sh("docker", "exec", name, "sh", "-c", "cat /tmp/slack-requests.jsonl 2>/dev/null || true", check=False)
    return [json.loads(line) for line in res.stdout.splitlines() if line.strip()]


def run(name: str, port: int, image: str) -> None:
    sh(*docker_run_args(name, port, image))
    wait_for(lambda: health(port), "GET /health to answer")
    print(f"PASS  /health answers: {health(port)}")
    uid = sh("docker", "exec", name, "id", "-u").stdout.strip()
    if uid == "0":
        raise SmokeFailure("the container runs as root")
    print(f"PASS  the container runs as uid {uid}, not root")

    sh("docker", "exec", "-d", name, "python", "/smoke/tools/smoke_fake.py")
    probe = f"import socket; socket.create_connection(('127.0.0.1', {FAKE_PORT}), 1)"
    wait_for(lambda: sh("docker", "exec", name, "python", "-c", probe, check=False).returncode == 0, "the fake Slack inside the container", 30.0)

    status, body = post_lead(port, "smoke-1")
    if status != 200 or not body or body.get("duplicate") is not False:
        raise SmokeFailure(f"POST /ingest answered {status} {body}")
    print(f"PASS  POST /ingest stored the lead as event {body.get('event_id')}")
    wait_for(lambda: (health(port) or {}).get("deliveries", {}).get("done") == 1, "the delivery to reach done")
    lines = fake_lines(name)
    if len(lines) != 1 or LEAD_NAME not in json.dumps(lines[0]):
        raise SmokeFailure(f"the fake Slack saw {len(lines)} messages and not exactly one with the lead: {lines}")
    print("PASS  the inline worker delivered the lead to the fake Slack exactly once")

    status, body = post_lead(port, "smoke-1")
    if status != 200 or not body or body.get("duplicate") is not True:
        raise SmokeFailure(f"the repeated delivery answered {status} {body}, not a duplicate")
    after = health(port) or {}
    if after.get("suppressed") != 1 or len(fake_lines(name)) != 1:
        raise SmokeFailure(f"a duplicate must post nothing more: {after}, {len(fake_lines(name))} messages")
    print("PASS  the same delivery again is a duplicate and posts nothing more")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="docker build the image first")
    ap.add_argument("--image", default=IMAGE)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--keep", action="store_true", help="leave the container running afterwards (for reading its logs)")
    args = ap.parse_args(argv)
    name = f"relay-smoke-{os.getpid()}"
    try:
        if args.build:
            sh("docker", "build", "-t", args.image, str(RELAY))
        run(name, args.port, args.image)
    except SmokeFailure as exc:
        print(f"FAIL  {exc}")
        logs = sh("docker", "logs", "--tail", "40", name, check=False)
        print("---- container log, last 40 lines ----\n" + (logs.stdout + logs.stderr))
        return 1
    finally:
        if not args.keep:
            sh("docker", "rm", "-f", name, check=False)
    print("container smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
