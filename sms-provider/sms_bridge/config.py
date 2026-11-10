"""Settings from environment variables. A secret is never in repr, so it cannot reach a log line by accident."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    bridge_token: str = field(repr=False)
    gateway_url: str
    gateway_user: str = field(repr=False)
    gateway_password: str = field(repr=False)
    sim_number: int | None = None
    highlevel_token: str | None = field(default=None, repr=False)
    highlevel_base: str = "https://services.leadconnectorhq.com"
    highlevel_version: str = "2021-04-15"
    host: str = "127.0.0.1"
    port: int = 8787
    store_path: str = "sms_store.json"
    poll_interval: float = 1.0
    poll_deadline: float = 60.0


def _number(env: dict, name: str, cast, default):
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError("%s must be a number" % name) from None
    if value <= 0:
        raise ConfigError("%s must be above zero" % name)
    return value


def from_env(env: dict | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    missing = [n for n in ("BRIDGE_TOKEN", "GATEWAY_URL", "GATEWAY_USER", "GATEWAY_PASSWORD") if not env.get(n, "").strip()]
    if missing:
        raise ConfigError("missing environment variables: " + ", ".join(missing))
    if len(env["BRIDGE_TOKEN"]) < 16:
        raise ConfigError("BRIDGE_TOKEN must be at least 16 characters")
    return Config(
        bridge_token=env["BRIDGE_TOKEN"],
        gateway_url=env["GATEWAY_URL"].strip().rstrip("/"),
        gateway_user=env["GATEWAY_USER"],
        gateway_password=env["GATEWAY_PASSWORD"],
        sim_number=_number(env, "SIM_NUMBER", int, None),
        highlevel_token=env.get("HIGHLEVEL_ACCESS_TOKEN", "").strip() or None,
        highlevel_base=env.get("HIGHLEVEL_API_BASE", "").strip().rstrip("/") or Config.highlevel_base,
        highlevel_version=env.get("HIGHLEVEL_API_VERSION", "").strip() or Config.highlevel_version,
        host=env.get("BRIDGE_HOST", "").strip() or Config.host,
        port=_number(env, "BRIDGE_PORT", int, Config.port),
        store_path=env.get("STORE_PATH", "").strip() or Config.store_path,
        poll_interval=_number(env, "POLL_INTERVAL", float, Config.poll_interval),
        poll_deadline=_number(env, "POLL_DEADLINE", float, Config.poll_deadline),
    )
