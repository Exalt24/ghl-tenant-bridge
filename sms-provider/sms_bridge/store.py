"""A small JSON file keyed by HighLevel's messageId. It is how a replayed message is told from a new one."""

from __future__ import annotations

import json
import os
import threading
import time


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self._data = json.load(fh)

    def _flush(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def claim(self, message_id: str) -> tuple[bool, dict]:
        """Take a message for sending. False and the stored record when it was seen before.

        A record the gateway refused (marked retryable) is taken again, because nothing was queued on the phone.
        """
        with self._lock:
            old = self._data.get(message_id)
            if old is not None and not old.get("retryable"):
                return False, dict(old)
            rec = {"status": "sending", "claimedAt": int(time.time()), "attempts": (old or {}).get("attempts", 0) + 1}
            self._data[message_id] = rec
            self._flush()
            return True, dict(rec)

    def update(self, message_id: str, **fields) -> dict:
        with self._lock:
            rec = self._data[message_id]
            rec.update(fields)
            self._flush()
            return dict(rec)

    def get(self, message_id: str) -> dict | None:
        with self._lock:
            rec = self._data.get(message_id)
            return dict(rec) if rec is not None else None
