"""Runs the Slack fake INSIDE the relay container, so the container smoke test can send a lead through the real image to a vendor
that is not the real vendor.

Started by container_smoke.py with `docker exec`. The relay folder is mounted read only at /smoke, so this imports the same
`tests/fakes/slack.py` the suite uses. Every message the fake accepts is appended to a JSON lines file the smoke test reads back.
"""

from __future__ import annotations

import json
import sys

PORT = 8081
LOG = "/tmp/slack-requests.jsonl"


def main() -> int:
    sys.path.append(sys.argv[1] if len(sys.argv) > 1 else "/smoke")
    from tests.fakes.slack import FakeSlack
    from werkzeug.serving import make_server

    fake = FakeSlack()

    def app(environ, start_response):  # type: ignore[no-untyped-def]
        before = fake.effects
        response = fake(environ, start_response)
        if fake.effects > before:
            with open(LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(fake.requests[-1]["json"]) + "\n")
        return response

    make_server("127.0.0.1", PORT, app, threaded=True).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
