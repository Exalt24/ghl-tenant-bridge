# LIFESPAN: permanent
"""The container half of the Fargate task's filesystem shape, run against the real image. Needs Docker and nothing else.

    python tests/container_readonly_check.py

The task runs the relay as uid 10001 on a READ ONLY root filesystem with an empty task volume mounted at /tmp. Terraform's mocked tests only see the task
definition JSON, so this builds relay/Dockerfile and shows what the container does in the three situations that matter:

  1. control: /tmp is a read only mount        -> the container must exit with "No usable temporary directory" (so the other two cannot pass by accident)
  2. /tmp is the image's own VOLUME             -> /tmp inside the volume is mode 1777 and the container answers /health
  3. /tmp is a tmpfs, as relay/tools/container_smoke.py runs it -> the container answers /health

Case 2 stands in for ECS, which copies the image's /tmp (and its permissions) into the task volume only because the Dockerfile declares VOLUME ["/tmp"].
It is NOT a Fargate run: Fargate's own volume handling is unverified until a task runs on a real account.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY = os.path.abspath(os.path.join(HERE, "..", "..", "..", "relay"))
IMAGE = "relay-readonly-check"
ENV = ["-e", "RELAY_ENV=dev", "-e", "RELAY_INGEST_TOKEN=check-token-1234567890123456", "-e", "RELAY_INLINE_WORKER=1", "-e", "WEB_CONCURRENCY=1", "-e", "RELAY_CONFIG={}", "-e", "HOME=/tmp"]


def sh(*args, check=True, timeout=600):
    res = subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if check and res.returncode != 0:
        raise RuntimeError("%s failed: %s" % (" ".join(args[:3]), (res.stderr or res.stdout)[-400:]))
    return res


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def healthy(port, seconds=40):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=2) as resp:
                if resp.status == 200 and json.loads(resp.read()).get("ok") is True:
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    return False


def run_case(extra):
    name = "ro-check-" + uuid.uuid4().hex[:8]
    port = free_port()
    sh("docker", "run", "-d", "--name", name, "--read-only", "--tmpfs", "/data:uid=10001", *extra, *ENV, "-p", "%d:8000" % port, IMAGE)
    try:
        ok = healthy(port, 40)
        state = sh("docker", "inspect", "-f", "{{.State.Status}}", name).stdout.strip()
        logs = sh("docker", "logs", name, check=False)
        return ok, state, (logs.stdout + logs.stderr)
    finally:
        sh("docker", "rm", "-f", "-v", name, check=False)


def main():
    sh("docker", "build", "-q", "-t", IMAGE, RELAY)
    failures = []

    ok, state, logs = run_case(["--tmpfs", "/tmp:ro"])
    good = (not ok) and "No usable temporary directory" in logs
    print(("ok   " if good else "FAIL ") + "control: a read only /tmp makes the container exit with 'No usable temporary directory'")
    if not good:
        failures.append("control")

    mode = sh("docker", "run", "--rm", "--read-only", IMAGE, "stat", "-c", "%a", "/tmp").stdout.strip()
    ok, state, logs = run_case([])
    good = mode == "1777" and ok and state == "running"
    print(("ok   " if good else "FAIL ") + "the image's own /tmp volume is mode %s and the container answers /health (%s)" % (mode, state))
    if not good:
        failures.append("volume")

    ok, state, logs = run_case(["--tmpfs", "/tmp"])
    good = ok and state == "running"
    print(("ok   " if good else "FAIL ") + "a tmpfs /tmp: the container answers /health (%s)" % state)
    if not good:
        failures.append("tmpfs")

    print("READONLY CHECK " + ("FAIL: " + ", ".join(failures) if failures else "PASS"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
