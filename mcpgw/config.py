"""Configuration loading (TOML, stdlib only) with ${ENV} interpolation."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("mcpgw.config")

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SERVER_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
RESERVED_NAMES = {"gateway"}

# Keys every server understands; anything else is passed to the provider as an option.
_SERVER_KEYS = {
    "provider", "url", "auth", "token", "client_id", "client_secret",
    "token_endpoint_auth_method", "scopes", "headers", "timeout", "tools",
    "protocol", "enabled", "strict_pkce", "display_name",
}


class ConfigError(ValueError):
    pass


@dataclass
class ServerConfig:
    name: str
    provider: str = "generic"
    display_name: str | None = None
    url: str | None = None
    auth: str = "oauth"  # oauth | bearer | none
    token: str | None = None  # for auth = "bearer"
    client_id: str | None = None  # pre-registered OAuth client
    client_secret: str | None = None
    token_endpoint_auth_method: str | None = None
    scopes: list[str] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 60.0
    tools_allow: list[str] = field(default_factory=list)
    tools_deny: list[str] = field(default_factory=list)
    protocol: str = "auto"  # auto | modern | legacy
    enabled: bool = True
    strict_pkce: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    public_url: str = ""
    api_key: str | None = None
    state_dir: Path = Path("~/.mcpgw")
    separator: str = "__"
    allowed_origins: list[str] = field(default_factory=list)
    client_metadata_url: str | None = None  # CIMD; must be an https URL others can fetch
    plugins: list[str] = field(default_factory=list)
    expose_connect_tools: bool = True
    auth_wait_seconds: float = 90.0
    tool_list_ttl: float = 300.0
    servers: dict[str, ServerConfig] = field(default_factory=dict)

    @property
    def redirect_uri(self) -> str:
        return self.public_url.rstrip("/") + "/oauth/callback"

    @property
    def public_origin(self) -> str:
        u = urlsplit(self.public_url)
        return f"{u.scheme}://{u.netloc}"

    @property
    def cimd_url(self) -> str | None:
        """The client_id to use for Client ID Metadata Documents, if usable."""
        if self.client_metadata_url:
            return self.client_metadata_url
        if self.public_url.startswith("https://"):
            return self.public_url.rstrip("/") + "/oauth/client-metadata.json"
        return None


def is_loopback_host(host: str) -> bool:
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_secure_url(url: str, what: str) -> None:
    """OAuth endpoints MUST be https; plain http is only tolerated on loopback."""
    u = urlsplit(url)
    if u.scheme == "https":
        return
    if u.scheme == "http" and is_loopback_host(u.hostname or ""):
        return
    raise ConfigError(f"{what} must use https: {url}")


def _interpolate(value: Any, missing: set[str]) -> Any:
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            missing.add(name)
            return ""
        return _ENV.sub(sub, value)
    if isinstance(value, list):
        return [_interpolate(v, missing) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate(v, missing) for k, v in value.items()}
    return value


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _str_list(v: Any, what: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [s for s in v.split() if s]
    if isinstance(v, list) and all(isinstance(s, str) for s in v):
        return [s for s in v if s]
    raise ConfigError(f"{what} must be a list of strings")


def parse_config(raw: dict[str, Any]) -> GatewayConfig:
    missing: set[str] = set()
    raw = _interpolate(raw, missing)
    gw = raw.get("gateway", {}) or {}
    cfg = GatewayConfig()
    cfg.host = str(gw.get("host", cfg.host))
    cfg.port = int(gw.get("port", cfg.port))
    cfg.public_url = (_str_or_none(gw.get("public_url")) or f"http://localhost:{cfg.port}").rstrip("/")
    cfg.api_key = _str_or_none(gw.get("api_key"))
    cfg.state_dir = Path(os.path.expanduser(str(gw.get("state_dir", cfg.state_dir))))
    cfg.separator = str(gw.get("separator", cfg.separator))
    cfg.allowed_origins = _str_list(gw.get("allowed_origins"), "gateway.allowed_origins")
    cfg.client_metadata_url = _str_or_none(gw.get("client_metadata_url"))
    cfg.plugins = _str_list(gw.get("plugins"), "gateway.plugins")
    cfg.expose_connect_tools = bool(gw.get("expose_connect_tools", True))
    cfg.auth_wait_seconds = float(gw.get("auth_wait_seconds", cfg.auth_wait_seconds))
    cfg.tool_list_ttl = float(gw.get("tool_list_ttl", cfg.tool_list_ttl))

    if not cfg.separator or not re.fullmatch(r"[A-Za-z0-9_.\-]+", cfg.separator):
        raise ConfigError("gateway.separator must be non-empty and use only [A-Za-z0-9_.-]")
    if cfg.client_metadata_url and not cfg.client_metadata_url.startswith("https://"):
        raise ConfigError("gateway.client_metadata_url must be an https URL with a path")
    pu = urlsplit(cfg.public_url)
    if pu.scheme not in ("http", "https") or not pu.netloc:
        raise ConfigError("gateway.public_url must be an absolute http(s) URL")
    if pu.scheme == "http" and not is_loopback_host(pu.hostname or ""):
        raise ConfigError("gateway.public_url must be https unless it is a loopback address "
                          "(OAuth redirect URIs must be localhost or https)")
    if not is_loopback_host(cfg.host) and not cfg.api_key:
        raise ConfigError(f"refusing to bind {cfg.host} without gateway.api_key; "
                          "set one or bind to 127.0.0.1")

    for name, s in (raw.get("servers") or {}).items():
        if not _SERVER_NAME.match(name) or name in RESERVED_NAMES:
            raise ConfigError(f"invalid server name {name!r}: use [a-z0-9-], max 32 chars, not 'gateway'")
        if cfg.separator in name:
            raise ConfigError(f"server name {name!r} must not contain the separator {cfg.separator!r}")
        if not isinstance(s, dict):
            raise ConfigError(f"servers.{name} must be a table")
        tools = s.get("tools") or {}
        sc = ServerConfig(
            name=name,
            provider=str(s.get("provider", "generic")),
            display_name=_str_or_none(s.get("display_name")),
            url=_str_or_none(s.get("url")),
            auth=str(s.get("auth", "")).strip() or "",
            token=_str_or_none(s.get("token")),
            client_id=_str_or_none(s.get("client_id")),
            client_secret=_str_or_none(s.get("client_secret")),
            token_endpoint_auth_method=_str_or_none(s.get("token_endpoint_auth_method")),
            scopes=_str_list(s["scopes"], f"servers.{name}.scopes") if "scopes" in s else None,
            headers={str(k): str(v) for k, v in (s.get("headers") or {}).items()},
            timeout=float(s.get("timeout", 60.0)),
            tools_allow=_str_list(tools.get("allow"), f"servers.{name}.tools.allow"),
            tools_deny=_str_list(tools.get("deny"), f"servers.{name}.tools.deny"),
            protocol=str(s.get("protocol", "auto")),
            enabled=bool(s.get("enabled", True)),
            strict_pkce=bool(s.get("strict_pkce", True)),
            options={k: v for k, v in s.items() if k not in _SERVER_KEYS},
        )
        if sc.protocol not in ("auto", "modern", "legacy"):
            raise ConfigError(f"servers.{name}.protocol must be auto, modern or legacy")
        if sc.auth and sc.auth not in ("oauth", "bearer", "none"):
            raise ConfigError(f"servers.{name}.auth must be oauth, bearer or none")
        cfg.servers[name] = sc

    if missing:
        log.warning("unset environment variables referenced in config: %s", ", ".join(sorted(missing)))
    return cfg


def load_config(path: str | os.PathLike | None = None) -> GatewayConfig:
    path = Path(path or os.environ.get("MCPGW_CONFIG", "mcpgw.toml"))
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with open(path, "rb") as f:
        return parse_config(tomllib.load(f))
