#!/usr/bin/env python3
# LIFESPAN: permanent
"""Proof that graphql/noop.sh cannot stop the database container on a Linux host, and a table of the whole class. Needs Docker; standard library only.

    python graphql/check_noop_init.py           the real file at both modes, then the full table (10 containers, a few minutes)
    python graphql/check_noop_init.py --quick   only the control and the real file at mode 0644

The postgres entrypoint runs an init file directly when it is executable and SOURCES it when it is not. Git on Windows stores noop.sh as mode 644, and a bind
mount from Windows shows every file as executable, so a run on Windows can never show what a Linux checkout does. This check puts a file in a Docker volume at an
exact mode, under the name of the image's own init script, and starts the real postgis image.

The class is "an init script that ends the shell it runs in". A script ending in `exit` ends the entrypoint when it is SOURCED (mode 0644) and is harmless when it is
EXECUTED (mode 0755), except that `exit 1` fails the init either way. A script that does nothing survives both. The table below is generated from those two axes.
"""
import base64
import itertools
import os
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE = "postgis/postgis:17-3.5"


def sh(*args, check=True):
    res = subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if check and res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout)[-300:])
    return res


def survives(body, mode):
    """True when the real postgis container finishes its first-start init and keeps running with `body` installed as an init script of file mode `mode`."""
    vol = "noopmode_" + uuid.uuid4().hex[:6]
    name = "noopmode_c_" + uuid.uuid4().hex[:6]
    sh("docker", "volume", "create", vol)
    encoded = base64.b64encode(body).decode()
    # written inside the volume through a shell at the exact mode, so no host file mode is involved
    sh("docker", "run", "--rm", "-v", vol + ":/d", "alpine", "sh", "-c", "echo %s | base64 -d > /d/10_postgis.sh && chmod %s /d/10_postgis.sh" % (encoded, mode))
    sh("docker", "run", "-d", "--name", name, "-e", "POSTGRES_PASSWORD=pw", "-v", vol + ":/docker-entrypoint-initdb.d", IMAGE)
    status, complete = "running", False
    deadline = time.time() + 90
    try:
        while time.time() < deadline:
            status = sh("docker", "inspect", "-f", "{{.State.Status}}", name).stdout.strip()
            text = sh("docker", "logs", name, check=False)
            text = text.stdout + text.stderr
            if status != "running":
                break
            if "PostgreSQL init process complete" in text:
                complete = True
                break
            time.sleep(0.3)
    finally:
        sh("docker", "rm", "-f", "-v", name, check=False)
        sh("docker", "volume", "rm", "-f", vol, check=False)
    return status == "running" and complete


def expected_to_survive(body, mode):
    """What the entrypoint's rule predicts: an exit 1 always fails the init; an exit 0 ends the shell only when the file is sourced (not executable)."""
    text = body.decode()
    if "exit 1" in text:
        return False
    if "exit" in text:
        return mode == "755"
    return True


def main(argv):
    with open(os.path.join(HERE, "noop.sh"), "rb") as fh:
        real = fh.read()
    problems = []

    def check(label, body, mode, want):
        got = survives(body, mode)
        print("%-52s mode %s: %s%s" % (label, mode, "survives" if got else "stops   ", "" if got == want else "   UNEXPECTED, wanted " + ("survives" if want else "stops")))
        if got != want:
            problems.append("%s at %s" % (label, mode))

    check("control: a script with exit 0", b"#!/bin/sh\nexit 0\n", "644", False)
    check("graphql/noop.sh as it is", real, "644", True)
    if "--quick" not in argv:
        check("graphql/noop.sh as it is", real, "755", True)
        # CLASS-TABLE: init script ends the shell it runs in; vocabulary = four script bodies x two file modes (the dimensions that decide the outcome)
        bodies = {"exit 0": b"#!/bin/sh\nexit 0\n", "exit 1": b"#!/bin/sh\nexit 1\n", "a colon": b"#!/bin/sh\n:\n", "comments only": b"#!/bin/sh\n# does nothing\n:\n"}
        for (label, body), mode in itertools.product(bodies.items(), ("644", "755")):
            check("table: " + label, body, mode, expected_to_survive(body, mode))
    print("NOOP INIT CHECK " + ("PASS" if not problems else "FAIL: " + "; ".join(problems)))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
