"""Keeps secrets out of log lines.

Two layers. First, any URL query value named api_key, access_token or token is masked, and a Bearer value too. Second,
every secret the config resolved (hook URLs, tokens, shared secrets) is registered and masked by VALUE wherever it
appears. The mask runs in the log record factory rather than on one handler, so a record made by any logger, ours or a
library's, is cleaned before any handler sees it.
"""

from __future__ import annotations

import logging
import re
import threading

MASK = "***"
_MIN_SECRET_LEN = 4

_lock = threading.Lock()
_secrets: set[str] = set()
_QUERY = re.compile(r"(?i)([?&;](?:api_key|access_token|token)=)[^&\s\"'<>]+")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")


def register(*values: str | None) -> None:
    with _lock:
        for v in values:
            if v and len(v) >= _MIN_SECRET_LEN:
                _secrets.add(v)


def forget_all() -> None:
    """Tests only: start from an empty registry."""
    with _lock:
        _secrets.clear()


def mask(text: str) -> str:
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for s in secrets:
        text = text.replace(s, MASK)
    text = _QUERY.sub(lambda m: m.group(1) + MASK, text)
    return _BEARER.sub(lambda m: m.group(1) + MASK, text)


_installed = False
_original_factory = None


def install() -> None:
    """Idempotent. Wraps the record factory once and quiets the HTTP libraries."""
    global _installed, _original_factory
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if _installed:
        return
    original = _original_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        record = original(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = mask(message)
        record.args = None
        if record.exc_info and record.exc_info[0] is not None and not record.exc_text:
            record.exc_text = mask(logging.Formatter().formatException(record.exc_info))
        return record

    logging.setLogRecordFactory(factory)
    _installed = True


def uninstall() -> None:
    """Tests only: put the original record factory back, so a test can prove that creating the app installs the mask."""
    global _installed
    if _installed and _original_factory is not None:
        logging.setLogRecordFactory(_original_factory)
    _installed = False
