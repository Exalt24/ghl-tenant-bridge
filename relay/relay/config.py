"""Configuration: a JSON file of NAMES, never values.

Every secret or URL in the JSON is the name of an environment variable (`url_env`, `workflow_secret_env`), resolved when
the config loads. A literal secret in the JSON is refused by name so it cannot reach git by accident.

Guards, each with a test and a mutant:
  RELAY_LIVE   a non-local base URL (a real vendor) is refused unless RELAY_LIVE=1, so a test or a dev run can never
               post to a real Slack channel by a stray environment variable.
  RELAY_ENV    dev | test | prod, unset means prod (fail closed). The Ed25519 test key override is honoured only when
               it is exactly `test`. SQLite is refused in prod unless the worker shares the container and the file
               sits on a path the operator says is a mounted volume; a separate `worker --once` service needs Postgres.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse

from . import contacts, redact
from .fathom_hook import parse_secret
from .ghl import GHL_ED25519_SPKI_B64
from .sinks import registry

ENVS = ("dev", "test", "prod")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MIN_TOKEN_CHARS = 16
DEFAULT_DEDUPE_WINDOW_S = 3600.0
DEFAULT_MAX_BODY_BYTES = 256 * 1024
FATHOM_MAX_BODY_BYTES = 2 * 1024 * 1024  # a transcript-enabled payload can be large, a 413 would make Fathom retry
FATHOM_TOLERANCE_S = 300.0  # Fathom's page says "typically 5 minutes"
DEFAULT_PULL_DAYS = 28
MAX_PULL_DAYS = 365

LITERAL_SECRET_KEYS = {"url", "webhook_url", "token", "secret", "apikey", "api_key", "password", "workflow_secret"}
SINK_KEYS = {"enabled", "url_env", "apikey_env", "timeout_s", "min_interval_s", "pause_s", "max_age_s", "env", "options"}
TENANT_KEYS = {"workflow_secret_env", "dedupe_window_s", "dedupe_mode", "max_body_bytes", "sinks", "ghl_timestamp_window", "fathom", "pulls"}
FATHOM_KEYS = {"secret_env", "tolerance_s", "max_body_bytes", "slack"}
# What each pull may name. `*_env` keys hold the NAME of an environment variable, never a value.
PULL_KEYS = {
    "meta_insights": {"base_url_env", "token_env", "account_env", "days", "limit", "timeout_s"},
    "windsor": {"base_url_env", "api_key_env", "label", "days", "timeout_s"},
    "fathom": {"base_url_env", "api_key_env", "days", "include_summary", "timeout_s"},
}
PULL_REQUIRED_ENV = {
    "meta_insights": ("base_url_env", "token_env", "account_env"),
    "windsor": ("base_url_env", "api_key_env"),
    "fathom": ("base_url_env", "api_key_env"),
}
LABEL_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
TOP_KEYS = {"locations", "tenants", "alert_webhook_env", "max_body_bytes", "dedupe_window_s", "ghl_webhook_max_body_bytes"}
# Identifiers and key material that belong in the environment, never in a file under git (a pixel id and a spreadsheet
# id are account identifiers, a service account carries a private key). Refused by name inside a sink's `options`.
LITERAL_OPTION_KEYS = LITERAL_SECRET_KEYS | {
    "access_token", "private_key", "service_account", "pixel_id", "spreadsheet_id", "client_email", "account_id",
}
ROLES = ("web", "worker", "worker_once")


class ConfigError(Exception):
    """A readable reason. Never carries a secret value or a URL."""


@dataclass
class SinkCfg:
    name: str
    enabled: bool
    url: str
    apikey: str | None
    timeout_s: float
    min_interval_s: float
    pause_s: float
    max_age_s: float
    # The literal options plus the values of the environment variables the sink's `env` map named, as the sink reads
    # them from its context. `secrets` is every value among them that must never reach a log line or a database column.
    extra: dict[str, Any] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)


@dataclass
class FathomCfg:
    """The Fathom webhook receiver for one tenant. `secret` is the `whsec_...` value from Fathom, resolved from the environment."""

    secret: str
    tolerance_s: float
    max_body_bytes: int
    slack: bool


@dataclass
class PullCfg:
    """One ad spend or meeting pull. `key` is the access token or API key (a secret), `account` the ad account or label."""

    name: str
    base_url: str
    key: str
    account: str
    days: int
    timeout_s: float
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TenantCfg:
    name: str
    workflow_secret: str | None
    dedupe_window_s: float
    dedupe_mode: str  # "event_key" (default) or "body_hash"
    max_body_bytes: int
    sinks: dict[str, SinkCfg] = field(default_factory=dict)
    # ASSUMED rule, off by default: refuse a direct HighLevel delivery whose `timestamp` is older than 30 days or more
    # than 5 minutes ahead. HighLevel retries up to 12 times and what `timestamp` means on a retry is undocumented.
    ghl_timestamp_window: bool = False
    fathom: FathomCfg | None = None
    pulls: dict[str, PullCfg] = field(default_factory=dict)

    def enabled_sinks(self) -> list[str]:
        return sorted(n for n, s in self.sinks.items() if s.enabled)

    def meeting_sinks(self) -> list[str]:
        """The sinks a Fathom meeting goes to: Slack, and only when the tenant's fathom block says `slack: true`."""
        return ["slack"] if self.fathom is not None and self.fathom.slack else []


@dataclass
class Config:
    env: str
    live: bool
    role: str
    database_url: str | None
    db_path: str | None
    inline_worker: bool
    worker_interval_s: float
    ingest_token: str | None
    ingest_token_previous: str | None
    admin_token: str | None
    alert_url: str | None
    ghl_public_key_b64: str
    max_body_bytes: int
    locations: dict[str, str]
    tenants: dict[str, TenantCfg]
    ghl_webhook_max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    # None means the fan-out default (300 s). A lease is never shorter than three sink timeouts whatever this says.
    lease_min_s: float | None = None
    # How a phone with no country is read, by the send time contact check and by the history import (relay/contacts.py).
    default_region: str = "US"

    def tenant_for_location(self, location_id: Any) -> str | None:
        return self.locations.get(location_id) if isinstance(location_id, str) else None

    def secret_values(self) -> list[str]:
        vals: list[str | None] = [self.ingest_token, self.ingest_token_previous, self.admin_token, self.alert_url]
        for t in self.tenants.values():
            vals.append(t.workflow_secret)
            if t.fathom is not None:
                vals.append(t.fathom.secret)
            for p in t.pulls.values():
                vals.append(p.key)
            for s in t.sinks.values():
                vals.extend([s.url, s.apikey, *s.secrets])
        return [v for v in vals if v]


def check_base_url(url: str, live: bool, what: str) -> None:
    """Local fakes always pass. Anything else needs RELAY_LIVE=1 and https. The message names the setting, not the URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError(f"{what} is not an http(s) URL")
    if parsed.hostname in LOCAL_HOSTS:
        return
    if not live:
        raise ConfigError(f"{what} points at a non-local host and RELAY_LIVE is not 1")
    if parsed.scheme != "https":
        raise ConfigError(f"{what} must use https")


def _env_value(environ: Mapping[str, str], name: Any, what: str) -> str:
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{what} must name an environment variable")
    value = environ.get(name)
    if not value:
        raise ConfigError(f"{what}: environment variable {name} is not set")
    return value


def _check_keys(d: dict[str, Any], allowed: set[str], where: str) -> None:
    for k in d:
        if k in LITERAL_SECRET_KEYS:
            raise ConfigError(f"{where}: `{k}` would hold a secret in the file; use `{k}_env` and name an environment variable")
        if k not in allowed:
            raise ConfigError(f"{where}: unknown key `{k}`")


def _positive(d: dict[str, Any], key: str, default: float, where: str) -> float:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise ConfigError(f"{where}: `{key}` must be a positive number")
    return float(v)


def _flag(d: dict[str, Any], key: str, where: str) -> bool:
    v = d.get(key, False)
    if not isinstance(v, bool):
        raise ConfigError(f"{where}: `{key}` must be true or false")
    return v


def _parse_json(source: str) -> dict[str, Any]:
    text = source.strip()
    if not text.startswith("{"):
        try:
            with open(text, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            raise ConfigError("RELAY_CONFIG is neither JSON nor a readable file") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise ConfigError("RELAY_CONFIG is not valid JSON") from None
    if not isinstance(data, dict):
        raise ConfigError("RELAY_CONFIG must be a JSON object")
    return data


def _parse_fathom(env: Mapping[str, str], raw: Any, tname: str, sinks: dict[str, SinkCfg]) -> FathomCfg | None:
    if raw is None:
        return None
    where = f"tenant {tname} fathom"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be an object")
    _check_keys(raw, FATHOM_KEYS, where)
    secret = _env_value(env, raw.get("secret_env"), f"{where} secret_env")
    try:
        parse_secret(secret)
    except ValueError:
        raise ConfigError(f"{where}: the secret must be `whsec_` followed by base64 of at least 16 bytes") from None
    slack = _flag(raw, "slack", where)
    if slack and not (sinks.get("slack") and sinks["slack"].enabled):
        raise ConfigError(f"{where}: slack fan-out needs an enabled slack sink for the tenant")
    cap = int(_positive(raw, "max_body_bytes", FATHOM_MAX_BODY_BYTES, where))
    return FathomCfg(secret=secret, tolerance_s=_positive(raw, "tolerance_s", FATHOM_TOLERANCE_S, where), max_body_bytes=cap, slack=slack)


def _parse_pulls(env: Mapping[str, str], raw: Any, tname: str, live: bool) -> dict[str, PullCfg]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"tenant {tname} pulls must be an object")
    out: dict[str, PullCfg] = {}
    for pname, p in raw.items():
        where = f"tenant {tname} pull {pname}"
        if pname not in PULL_KEYS:
            raise ConfigError(f"tenant {tname}: unknown pull `{pname}`")
        if not isinstance(p, dict):
            raise ConfigError(f"{where} must be an object")
        _check_keys(p, PULL_KEYS[pname], where)
        values = {k: _env_value(env, p.get(k), f"{where} {k}") for k in PULL_REQUIRED_ENV[pname]}
        base_url = values["base_url_env"]
        check_base_url(base_url, live, f"{where} base url")
        key = values.get("token_env") or values["api_key_env"]
        account = values.get("account_env") or str(p.get("label", "default"))
        if pname == "windsor" and not LABEL_RE.fullmatch(account):
            raise ConfigError(f"{where}: label must be letters, digits, . _ - up to 64 characters")
        days = int(_positive(p, "days", DEFAULT_PULL_DAYS, where))
        if days > MAX_PULL_DAYS:
            raise ConfigError(f"{where}: days must be at most {MAX_PULL_DAYS}")
        extra: dict[str, Any] = {}
        if pname == "meta_insights":
            extra["limit"] = int(_positive(p, "limit", 500, where))
        if pname == "fathom":
            extra["include_summary"] = _flag(p, "include_summary", where)
        out[pname] = PullCfg(
            name=pname, base_url=base_url, key=key, account=account, days=days,
            timeout_s=_positive(p, "timeout_s", 30.0, where), extra=extra,
        )
    return out


def load_config(environ: Mapping[str, str] | None = None, role: str = "web") -> Config:
    env = dict(os.environ if environ is None else environ)
    if role not in ROLES:
        raise ConfigError("unknown role")

    relay_env = env.get("RELAY_ENV") or "prod"
    if relay_env not in ENVS:
        raise ConfigError("RELAY_ENV must be dev, test or prod")
    live = env.get("RELAY_LIVE") == "1"

    database_url = env.get("DATABASE_URL") or None
    db_path = env.get("RELAY_DB_PATH") or None
    inline_worker = env.get("RELAY_INLINE_WORKER") == "1"
    try:
        worker_interval_s = float(env.get("RELAY_WORKER_INTERVAL_S", "5"))
    except ValueError:
        raise ConfigError("RELAY_WORKER_INTERVAL_S must be a number") from None
    if worker_interval_s <= 0:
        raise ConfigError("RELAY_WORKER_INTERVAL_S must be positive")
    lease_min_s: float | None = None
    if env.get("RELAY_LEASE_MIN_S"):
        try:
            lease_min_s = float(env["RELAY_LEASE_MIN_S"])
        except ValueError:
            raise ConfigError("RELAY_LEASE_MIN_S must be a number") from None
        if not (lease_min_s >= 1.0) or lease_min_s == float("inf"):
            raise ConfigError("RELAY_LEASE_MIN_S must be at least 1 second")
    try:
        region = contacts.default_region(env)
    except contacts.ContactError as exc:
        raise ConfigError(str(exc)) from None
    if not database_url and relay_env == "prod":
        if role == "worker_once":
            raise ConfigError("a separate worker service needs Postgres: set DATABASE_URL")
        if not (db_path and os.path.isabs(db_path) and inline_worker):
            raise ConfigError(
                "SQLite is refused in prod unless RELAY_DB_PATH is an absolute path on a mounted volume "
                "and RELAY_INLINE_WORKER=1 runs the worker in this container; otherwise set DATABASE_URL"
            )

    raw = _parse_json(env.get("RELAY_CONFIG", "")) if env.get("RELAY_CONFIG") else {}
    _check_keys(raw, TOP_KEYS, "config")

    default_window = _positive(raw, "dedupe_window_s", DEFAULT_DEDUPE_WINDOW_S, "config")
    default_cap = int(_positive(raw, "max_body_bytes", DEFAULT_MAX_BODY_BYTES, "config"))
    ghl_cap = int(_positive(raw, "ghl_webhook_max_body_bytes", DEFAULT_MAX_BODY_BYTES, "config"))

    tenants: dict[str, TenantCfg] = {}
    for tname, t in (raw.get("tenants") or {}).items():
        if not TENANT_RE.match(tname):
            raise ConfigError(f"tenant name `{tname}` must be lowercase letters, digits, - or _")
        if not isinstance(t, dict):
            raise ConfigError(f"tenant {tname} must be an object")
        _check_keys(t, TENANT_KEYS, f"tenant {tname}")
        mode = t.get("dedupe_mode", "event_key")
        if mode not in ("event_key", "body_hash"):
            raise ConfigError(f"tenant {tname}: dedupe_mode must be event_key or body_hash")
        secret = None
        if "workflow_secret_env" in t:
            secret = _env_value(env, t["workflow_secret_env"], f"tenant {tname} workflow_secret_env")
            if len(secret) < MIN_TOKEN_CHARS:
                raise ConfigError(f"tenant {tname}: the workflow secret must be at least {MIN_TOKEN_CHARS} characters")
        sinks: dict[str, SinkCfg] = {}
        for sname, s in (t.get("sinks") or {}).items():
            if sname not in registry.names():
                raise ConfigError(f"tenant {tname}: unknown sink `{sname}`")
            if not isinstance(s, dict):
                raise ConfigError(f"tenant {tname} sink {sname} must be an object")
            where = f"tenant {tname} sink {sname}"
            _check_keys(s, SINK_KEYS, where)
            enabled = s.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ConfigError(f"{where}: enabled must be true or false")
            url = _env_value(env, s.get("url_env"), f"{where} url_env")
            check_base_url(url, live, f"{where} url")
            cls = registry.SINK_CLASSES[sname]
            if cls.needs_apikey and "apikey_env" not in s:
                raise ConfigError(f"{where}: apikey_env is required (it names the variable holding the access token)")
            apikey = _env_value(env, s["apikey_env"], f"{where} apikey_env") if "apikey_env" in s else None
            env_map = s.get("env", {})
            if not isinstance(env_map, dict) or any(k not in cls.required_env for k in env_map):
                raise ConfigError(f"{where}: `env` may only name {', '.join(cls.required_env) or 'nothing'}")
            env_values: dict[str, str] = {}
            for need in cls.required_env:
                if need not in env_map:
                    raise ConfigError(f"{where}: env.{need} is required and names an environment variable")
                env_values[need] = _env_value(env, env_map[need], f"{where} env.{need}")
            options = s.get("options", {})
            if not isinstance(options, dict):
                raise ConfigError(f"{where}: options must be an object")
            for k in options:
                if k in LITERAL_OPTION_KEYS:
                    raise ConfigError(f"{where}: option `{k}` would hold an identifier or a secret in the file; use `env`")
            try:
                checked = cls.check_options(options, env_values, live)
            except ValueError as exc:
                raise ConfigError(f"{where}: {exc}") from None
            d = registry.defaults(sname)
            sinks[sname] = SinkCfg(
                name=sname,
                enabled=enabled,
                url=url,
                apikey=apikey,
                timeout_s=_positive(s, "timeout_s", d["timeout_s"], where),
                min_interval_s=_positive(s, "min_interval_s", d["min_interval_s"], where),
                pause_s=_positive(s, "pause_s", d["pause_s"], where),
                max_age_s=_positive(s, "max_age_s", d["max_age_s"], where),
                extra={**checked, **env_values},
                secrets=cls.secret_values(env_values),
            )
        tenants[tname] = TenantCfg(
            name=tname,
            workflow_secret=secret,
            dedupe_window_s=_positive(t, "dedupe_window_s", default_window, f"tenant {tname}"),
            dedupe_mode=mode,
            max_body_bytes=int(_positive(t, "max_body_bytes", default_cap, f"tenant {tname}")),
            sinks=sinks,
            ghl_timestamp_window=_flag(t, "ghl_timestamp_window", f"tenant {tname}"),
            fathom=_parse_fathom(env, t.get("fathom"), tname, sinks),
            pulls=_parse_pulls(env, t.get("pulls"), tname, live),
        )

    locations = raw.get("locations") or {}
    if not isinstance(locations, dict):
        raise ConfigError("locations must map a location id to a tenant")
    for loc, tenant in locations.items():
        if tenant not in tenants:
            raise ConfigError(f"location {loc} maps to unknown tenant {tenant}")

    ingest_token = env.get("RELAY_INGEST_TOKEN") or None
    previous = env.get("RELAY_INGEST_TOKEN_PREVIOUS") or None
    admin = env.get("RELAY_ADMIN_TOKEN") or None
    if role == "web":
        if not ingest_token:
            raise ConfigError("RELAY_INGEST_TOKEN is not set")
        for label, tok in (("RELAY_INGEST_TOKEN", ingest_token), ("RELAY_INGEST_TOKEN_PREVIOUS", previous), ("RELAY_ADMIN_TOKEN", admin)):
            if tok and len(tok) < MIN_TOKEN_CHARS:
                raise ConfigError(f"{label} must be at least {MIN_TOKEN_CHARS} characters")
        if admin and admin in (ingest_token, previous):
            raise ConfigError("RELAY_ADMIN_TOKEN must differ from the ingest tokens")

    alert_env = raw.get("alert_webhook_env", "ALERT_WEBHOOK")
    alert_url = env.get(alert_env) or None if isinstance(alert_env, str) else None
    if alert_url:
        check_base_url(alert_url, live, "the alert webhook")

    # The test key override exists so signature tests can sign with a key they hold. It is honoured only in test.
    ghl_key = GHL_ED25519_SPKI_B64
    override = env.get("RELAY_TEST_GHL_PUBLIC_KEY")
    if override and relay_env == "test":
        ghl_key = override

    cfg = Config(
        env=relay_env,
        live=live,
        role=role,
        database_url=database_url,
        db_path=db_path,
        inline_worker=inline_worker,
        worker_interval_s=worker_interval_s,
        ingest_token=ingest_token,
        ingest_token_previous=previous,
        admin_token=admin,
        alert_url=alert_url,
        ghl_public_key_b64=ghl_key,
        max_body_bytes=default_cap,
        locations=dict(locations),
        tenants=tenants,
        ghl_webhook_max_body_bytes=ghl_cap,
        lease_min_s=lease_min_s,
        default_region=region,
    )
    redact.register(*cfg.secret_values())
    return cfg
