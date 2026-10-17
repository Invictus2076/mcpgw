"""Provider plugins.

A provider is a small class that knows the quirks of one hosted MCP service:
its URL, optional headers, and anything its authorization server needs that
spec-compliant discovery can't figure out on its own. Everything else (PRM
discovery, AS metadata, CIMD/DCR, PKCE, refresh) is handled generically.

Third-party providers can be registered three ways:

1. ``@register`` in any module listed in ``gateway.plugins``
2. a ``mcpgw.providers`` entry point pointing at a Provider subclass
3. subclass + ``register()`` before building the Gateway programmatically
"""

from __future__ import annotations

import importlib
import logging
from importlib.metadata import entry_points
from typing import ClassVar

from ..config import ConfigError, ServerConfig

log = logging.getLogger("mcpgw.providers")


class Provider:
    #: registry key used in config (``provider = "..."``)
    name: ClassVar[str] = "generic"
    display_name: ClassVar[str] = "MCP server"
    default_url: ClassVar[str | None] = None
    default_auth: ClassVar[str] = "oauth"
    docs: ClassVar[str] = ""
    #: True when the provider's authorization server supports neither CIMD nor DCR,
    #: so the user must bring a pre-registered client_id (or a static token).
    needs_client_credentials: ClassVar[bool] = False
    #: Vetted authorization-server metadata. Used only when the AS that the
    #: server's protected-resource metadata points to has this exact issuer:
    #: discovered fields win, these fill gaps (or stand in if discovery fails).
    fallback_as_metadata: ClassVar[dict | None] = None

    def configure(self, sc: ServerConfig) -> None:
        """Resolve URL/headers/auth defaults from provider options. Mutates ``sc``."""
        if not sc.auth:
            sc.auth = self.default_auth
        if not sc.display_name:
            sc.display_name = self.display_name if self.name != "generic" else sc.name
        if not sc.url:
            sc.url = self.build_url(sc)
        if not sc.url:
            raise ConfigError(f"servers.{sc.name}: 'url' is required for provider {self.name!r}")
        for k, v in self.extra_headers(sc).items():
            sc.headers.setdefault(k, v)

    def build_url(self, sc: ServerConfig) -> str | None:
        return self.default_url

    def extra_headers(self, sc: ServerConfig) -> dict[str, str]:
        return {}

    def authorize_params(self, sc: ServerConfig) -> dict[str, str]:
        """Extra query parameters for the authorization request."""
        return {}

    def problems(self, sc: ServerConfig, redirect_uri: str) -> list[str]:
        """Human-readable configuration problems that block connecting."""
        if sc.auth == "bearer" and not sc.token:
            return ["auth = \"bearer\" needs a token"]
        return []

    def option(self, sc: ServerConfig, key: str, default=None):
        return sc.options.get(key, default)


_REGISTRY: dict[str, type[Provider]] = {}


def register(cls: type[Provider]) -> type[Provider]:
    if not (isinstance(cls, type) and issubclass(cls, Provider)):
        raise TypeError("register() expects a Provider subclass")
    if cls.name in _REGISTRY and _REGISTRY[cls.name] is not cls:
        log.warning("provider %r overridden by %s", cls.name, cls.__module__)
    _REGISTRY[cls.name] = cls
    return cls


def get_provider(name: str) -> Provider:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ConfigError(f"unknown provider {name!r}; available: {', '.join(sorted(_REGISTRY))}") from None


def available() -> dict[str, type[Provider]]:
    return dict(sorted(_REGISTRY.items()))


def load_plugins(modules: list[str]) -> None:
    for mod in modules:
        importlib.import_module(mod)  # modules register themselves via @register
    for ep in entry_points(group="mcpgw.providers"):
        try:
            obj = ep.load()
            if isinstance(obj, type) and issubclass(obj, Provider):
                register(obj)
        except Exception:  # a broken plugin shouldn't take the gateway down
            log.exception("failed to load provider plugin %s", ep.name)


register(Provider)
from . import builtin  # noqa: E402,F401  (registers the built-in providers)
