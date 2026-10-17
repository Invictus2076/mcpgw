"""OAuth 2.1 client for upstream MCP servers, per the MCP authorization spec.

Flow (see ``AuthManager``):

  401 + WWW-Authenticate ──► Protected Resource Metadata (RFC 9728)
        ──► Authorization Server metadata (RFC 8414 / OIDC discovery, issuer-validated)
        ──► client: pre-registered ▸ CIMD ▸ DCR (RFC 7591, deprecated fallback)
        ──► authorization code + PKCE S256 + resource (RFC 8707) + state
        ──► callback: state + iss (RFC 9207) validation ──► token exchange
        ──► refresh (serialized per server, rotation-aware) / step-up on 403
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import aiohttp

from . import __version__
from .config import ConfigError, GatewayConfig, is_loopback_host, require_secure_url
from .store import TokenStore

if TYPE_CHECKING:  # pragma: no cover
    from .upstream import Upstream

log = logging.getLogger("mcpgw.oauth")

PENDING_TTL = 600.0
REFRESH_SKEW = 60.0
MAX_METADATA_BYTES = 256 * 1024


class AuthRequired(Exception):
    """The user has to (re)authorize this upstream server in a browser."""

    def __init__(self, server: str, reason: str, *, step_up: bool = False):
        super().__init__(reason)
        self.server = server
        self.reason = reason
        self.step_up = step_up


class OAuthError(Exception):
    pass


# ---------------------------------------------------------------------------
# WWW-Authenticate parsing (RFC 7235 / 6750 / 9728)
# ---------------------------------------------------------------------------

@dataclass
class Challenge:
    scheme: str
    params: dict[str, str] = field(default_factory=dict)
    token68: str | None = None

    @property
    def scopes(self) -> list[str]:
        return self.params.get("scope", "").split()


def parse_www_authenticate(header: str | None) -> list[Challenge]:
    out: list[Challenge] = []
    if not header:
        return out
    s, i, n = header, 0, len(header)
    cur: Challenge | None = None
    expect_token68 = False  # right after a scheme, a bare token is its token68 credential
    while i < n:
        comma = False
        while i < n and s[i] in " \t,":
            comma = comma or s[i] == ","
            i += 1
        start = i
        while i < n and s[i] not in " \t,=":
            i += 1
        token = s[start:i]
        if not token:
            i += 1
            continue
        j = i
        while j < n and s[j] in " \t":
            j += 1
        if j < n and s[j] == "=" and not (j + 1 < n and s[j + 1] in "=,"):
            i = j + 1
            while i < n and s[i] in " \t":
                i += 1
            if i < n and s[i] == '"':
                i += 1
                buf = []
                while i < n and s[i] != '"':
                    if s[i] == "\\" and i + 1 < n:
                        i += 1
                    buf.append(s[i])
                    i += 1
                i += 1
                value = "".join(buf)
            else:
                vs = i
                while i < n and s[i] not in " \t,":
                    i += 1
                value = s[vs:i]
            if cur is None:
                cur = Challenge("")
                out.append(cur)
            cur.params[token.lower()] = value
            expect_token68 = False
        else:
            pad_start = i
            while i < n and s[i] == "=":  # token68 padding
                i += 1
            if cur is not None and expect_token68 and not comma:
                cur.token68 = token + s[pad_start:i]
                expect_token68 = False
            else:
                cur = Challenge(token)
                out.append(cur)
                expect_token68 = True
    return out


def bearer_challenge(header: str | None) -> Challenge | None:
    for c in parse_www_authenticate(header):
        if c.scheme.lower() == "bearer":
            return c
    return None


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def _origin(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme.lower()}://{u.netloc.lower()}"


def prm_urls(server_url: str) -> list[str]:
    u = urlsplit(server_url)
    path = u.path.rstrip("/")
    base = _origin(server_url)
    urls = []
    if path:
        urls.append(f"{base}/.well-known/oauth-protected-resource{path}")
    urls.append(f"{base}/.well-known/oauth-protected-resource")
    return urls


def as_metadata_urls(issuer: str) -> list[str]:
    u = urlsplit(issuer)
    path = u.path.rstrip("/")
    base = f"{u.scheme}://{u.netloc}"
    if path:
        return [
            f"{base}/.well-known/oauth-authorization-server{path}",
            f"{base}/.well-known/openid-configuration{path}",
            f"{base}{path}/.well-known/openid-configuration",
        ]
    return [f"{base}/.well-known/oauth-authorization-server", f"{base}/.well-known/openid-configuration"]


def canonical_resource(server_url: str) -> str:
    u = urlsplit(server_url)
    path = u.path if u.path not in ("", "/") else ""
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), path, u.query, ""))


def resource_allowed(server_url: str, resource: str) -> bool:
    """PRM ``resource`` must identify this server: same origin, path prefix."""
    if _origin(server_url) != _origin(resource):
        return False
    want = urlsplit(server_url).path.rstrip("/") + "/"
    have = urlsplit(resource).path.rstrip("/") + "/"
    return want.startswith(have)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# AuthManager
# ---------------------------------------------------------------------------

@dataclass
class AuthState:
    """Per-server, in-memory discovery and challenge state."""

    resource_metadata_url: str | None = None
    challenge_scopes: list[str] = field(default_factory=list)
    granted_scopes: list[str] = field(default_factory=list)
    prm: dict | None = None
    as_meta: dict | None = None
    resource: str | None = None
    discovered_at: float = 0.0
    last_error: str | None = None


@dataclass
class Pending:
    server: str
    verifier: str
    issuer: str
    iss_supported: bool
    token_endpoint: str
    resource: str
    scopes: list[str]
    client: dict
    created: float = field(default_factory=time.time)


class AuthManager:
    def __init__(self, cfg: GatewayConfig, store: TokenStore, http: aiohttp.ClientSession):
        self.cfg = cfg
        self.store = store
        self.http = http
        self.state: dict[str, AuthState] = {}
        self._pending: dict[str, Pending] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._connected: dict[str, asyncio.Event] = {}
        self._generation: dict[str, int] = {}

    # -- small helpers ------------------------------------------------------

    def st(self, server: str) -> AuthState:
        return self.state.setdefault(server, AuthState())

    def _lock(self, server: str) -> asyncio.Lock:
        return self._locks.setdefault(server, asyncio.Lock())

    def generation(self, server: str) -> int:
        """Incremented every time the user completes an authorization for ``server``."""
        return self._generation.get(server, 0)

    async def wait_connected(self, server: str, since: int, timeout: float) -> bool:
        """Wait until an authorization newer than generation ``since`` completes."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.generation(server) <= since:
            ev = self._connected.setdefault(server, asyncio.Event())
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(ev.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
        return True

    def _signal_connected(self, server: str) -> None:
        self._generation[server] = self.generation(server) + 1
        old = self._connected.get(server)
        self._connected[server] = asyncio.Event()  # fresh, unset event for future waiters
        if old:
            old.set()

    @staticmethod
    def token_key(server: str) -> str:
        return f"token|{server}"

    def client_key(self, issuer: str) -> str:
        return f"client|{issuer}|{self.cfg.redirect_uri}"

    @staticmethod
    def _local_dev(up: "Upstream") -> bool:
        u = urlsplit(up.url)
        return u.scheme == "http" and is_loopback_host(u.hostname or "")

    def _check_url(self, up: "Upstream", url: str, what: str) -> None:
        """All OAuth URLs must be https. Plain http on loopback is accepted only when
        the upstream MCP server is itself a local http server (development), so a
        remote server can't steer the gateway at services on its own loopback."""
        if urlsplit(url).scheme == "https":
            return
        if self._local_dev(up):
            require_secure_url(url, what)
            return
        raise OAuthError(f"{what} must use https: {url}")

    async def _get_json(self, url: str, what: str) -> dict | None:
        try:
            async with self.http.get(url, headers={"Accept": "application/json"},
                                     timeout=aiohttp.ClientTimeout(total=15), allow_redirects=False) as r:
                if r.status != 200:
                    return None
                raw = await r.content.read(MAX_METADATA_BYTES + 1)
                if len(raw) > MAX_METADATA_BYTES:
                    raise OAuthError(f"{what} too large")
                data = json.loads(raw)
                return data if isinstance(data, dict) else None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.debug("fetch %s failed: %s", url, e)
            return None

    # -- tokens -------------------------------------------------------------

    async def token(self, up: "Upstream") -> dict | None:
        return await self.store.get(self.token_key(up.name))

    async def access_token(self, up: "Upstream") -> str | None:
        """Current access token, refreshed proactively if it is about to expire."""
        tok = await self.token(up)
        if not tok:
            return None
        exp = tok.get("expires_at")
        if exp and exp - time.time() < REFRESH_SKEW:
            if tok.get("refresh_token"):
                tok = await self.refresh(up, stale=tok.get("access_token"))
            else:
                await self.store.delete(self.token_key(up.name))
                return None
        return tok.get("access_token") if tok else None

    async def on_challenge(self, up: "Upstream", status: int, www_authenticate: str | None,
                           sent_token: str | None) -> bool:
        """Handle a 401/403 from the upstream. Returns True if the request should be
        retried (token refreshed); otherwise raises AuthRequired."""
        st = self.st(up.name)
        ch = bearer_challenge(www_authenticate)
        if ch:
            if ch.params.get("resource_metadata"):
                st.resource_metadata_url = ch.params["resource_metadata"]
            if ch.scopes:
                st.challenge_scopes = ch.scopes
        if status == 403:
            if ch and ch.params.get("error") == "insufficient_scope":
                raise AuthRequired(up.name, "additional permissions required "
                                   f"({' '.join(ch.scopes) or 'unspecified scope'})", step_up=True)
            raise AuthRequired(up.name, "access forbidden by upstream")
        if sent_token:
            tok = await self.token(up)
            if tok and tok.get("access_token") == sent_token and tok.get("refresh_token"):
                await self.refresh(up, stale=sent_token, force=True)  # raises AuthRequired if terminal
                return True
            if tok and tok.get("access_token") != sent_token:
                return True  # someone refreshed concurrently; retry with the new one
            await self.store.delete(self.token_key(up.name))
            raise AuthRequired(up.name, "token rejected by upstream")
        raise AuthRequired(up.name, "authorization required")

    async def disconnect(self, server: str) -> None:
        await self.store.delete(self.token_key(server))
        st = self.st(server)
        st.granted_scopes = []
        st.challenge_scopes = []

    # -- discovery ----------------------------------------------------------

    async def discover(self, up: "Upstream", *, force: bool = False) -> AuthState:
        st = self.st(up.name)
        if st.as_meta and not force and time.time() - st.discovered_at < 3600:
            return st
        server_url = up.url
        # 1. Protected Resource Metadata
        prm = None
        candidates = ([st.resource_metadata_url] if st.resource_metadata_url else []) + prm_urls(server_url)
        for url in candidates:
            self._check_url(up, url, "protected resource metadata URL")
            prm = await self._get_json(url, "protected resource metadata URL")
            if prm:
                break
        fallback = up.provider.fallback_as_metadata
        if prm is None:
            if not fallback:
                raise OAuthError(f"no OAuth protected resource metadata found for {server_url}")
            log.info("%s: no PRM found, using provider fallback metadata", up.name)
            prm = {"resource": canonical_resource(server_url), "authorization_servers": [fallback["issuer"]]}
        resource = prm.get("resource") or canonical_resource(server_url)
        if not resource_allowed(server_url, resource):
            raise OAuthError(f"protected resource metadata names resource {resource!r}, "
                             f"which does not match {server_url}")
        servers = prm.get("authorization_servers") or []
        if not servers or not isinstance(servers, list):
            raise OAuthError("protected resource metadata lists no authorization_servers")
        # 2. Authorization server metadata (first AS that validates)
        as_meta, last_err = None, None
        for issuer in servers:
            try:
                self._check_url(up, issuer, "authorization server issuer")
                as_meta = await self._as_metadata(issuer, fallback)
                if as_meta:
                    break
            except (OAuthError, ConfigError) as e:
                last_err = e
        if not as_meta:
            raise OAuthError(f"could not load authorization server metadata ({last_err or servers})")
        for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
            if key != "registration_endpoint" and not as_meta.get(key):
                raise OAuthError(f"authorization server metadata is missing {key}")
            if as_meta.get(key):
                self._check_url(up, as_meta[key], key)
        methods = as_meta.get("code_challenge_methods_supported")
        if not methods:
            if up.cfg.strict_pkce:
                raise OAuthError("authorization server does not advertise PKCE support "
                                 "(code_challenge_methods_supported); refusing per MCP spec. "
                                 "Set strict_pkce = false for this server to override.")
            log.warning("%s: AS does not advertise PKCE; proceeding with S256 (strict_pkce=false)", up.name)
        elif "S256" not in methods:
            raise OAuthError("authorization server does not support PKCE S256")
        # Authorization server binding: tokens from a different issuer are discarded.
        tok = await self.token(up)
        if tok and tok.get("issuer") != as_meta["issuer"]:
            log.warning("%s: authorization server changed (%s -> %s); dropping old token",
                        up.name, tok.get("issuer"), as_meta["issuer"])
            await self.store.delete(self.token_key(up.name))
        st.prm, st.as_meta, st.resource, st.discovered_at = prm, as_meta, resource, time.time()
        return st

    async def _as_metadata(self, issuer: str, fallback: dict | None) -> dict | None:
        meta = None
        for url in as_metadata_urls(issuer):
            doc = await self._get_json(url, "authorization server metadata URL")
            if doc is None:
                continue
            if doc.get("issuer") != issuer:  # RFC 8414 §3.3: must be identical
                log.warning("rejecting AS metadata from %s: issuer %r != %r", url, doc.get("issuer"), issuer)
                continue
            meta = doc
            break
        if fallback and fallback.get("issuer") == issuer:
            meta = {**fallback, **(meta or {})}
            for k, v in fallback.items():  # fill fields the live document left empty
                if not meta.get(k):
                    meta[k] = v
        return meta

    # -- client registration -----------------------------------------------

    async def _client(self, up: "Upstream", as_meta: dict, scopes: list[str]) -> dict:
        issuer = as_meta["issuer"]
        sc = up.cfg
        supported = as_meta.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        # 1. pre-registered (bound to one issuer: never send them to a different AS)
        if sc.client_id:
            pinned = sc.options.get("issuer") or (
                (up.provider.fallback_as_metadata or {}).get("issuer") if up.provider.needs_client_credentials else None)
            if pinned and pinned != issuer:
                raise OAuthError(f"{up.display_name}'s authorization server is now {issuer}, but the "
                                 f"configured client_id was registered with {pinned}; refusing to use it")
            method = sc.token_endpoint_auth_method
            if not method:
                if sc.client_secret:
                    method = next((m for m in ("client_secret_basic", "client_secret_post") if m in supported),
                                  "client_secret_post")
                else:
                    method = "none"
            return {"client_id": sc.client_id, "client_secret": sc.client_secret, "method": method,
                    "source": "config", "issuer": issuer}
        if up.provider.needs_client_credentials:
            raise OAuthError(f"{up.display_name} requires a pre-registered client_id "
                             "(its authorization server has no dynamic registration)")
        # 2. Client ID Metadata Document
        cimd = self.cfg.cimd_url
        if cimd and as_meta.get("client_id_metadata_document_supported"):
            return {"client_id": cimd, "client_secret": None, "method": "none", "source": "cimd", "issuer": issuer}
        # 3. Dynamic Client Registration (deprecated fallback)
        reg_ep = as_meta.get("registration_endpoint")
        if not reg_ep:
            raise OAuthError("authorization server supports neither Client ID Metadata Documents "
                             "nor dynamic registration; configure client_id for this server")
        key = self.client_key(issuer)
        cached = await self.store.get(key)
        if cached:
            expires = cached.get("client_secret_expires_at") or 0  # 0 = never
            if expires == 0 or expires > time.time() + 60:
                return cached
        return await self._register(reg_ep, key, issuer, scopes)

    async def _register(self, endpoint: str, key: str, issuer: str, scopes: list[str]) -> dict:
        redirect = self.cfg.redirect_uri
        native = is_loopback_host(urlsplit(redirect).hostname or "")
        last = ""
        for app_type in (("native", "web") if native else ("web",)):
            body = {
                "client_name": "mcpgw (MCP Gateway)",
                "redirect_uris": [redirect],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "application_type": app_type,
                "software_id": "mcpgw",
                "software_version": __version__,
            }
            if scopes:
                body["scope"] = " ".join(scopes)
            async with self.http.post(endpoint, json=body, headers={"Accept": "application/json"},
                                      timeout=aiohttp.ClientTimeout(total=20), allow_redirects=False) as r:
                text = await r.text()
                if r.status in (200, 201):
                    data = await r.json(content_type=None)
                    if not data.get("client_id"):
                        raise OAuthError("registration response has no client_id")
                    method = data.get("token_endpoint_auth_method") or (
                        "client_secret_basic" if data.get("client_secret") else "none")
                    rec = {"client_id": data["client_id"], "client_secret": data.get("client_secret"),
                           "client_secret_expires_at": data.get("client_secret_expires_at") or 0,
                           "method": method, "source": "dcr", "issuer": issuer,
                           "registered_at": int(time.time())}
                    await self.store.put(key, rec)
                    log.info("registered OAuth client with %s (%s)", issuer, app_type)
                    return rec
                last = f"HTTP {r.status}: {text[:300]}"
                log.warning("dynamic client registration (%s) failed: %s", app_type, last)
        raise OAuthError(f"dynamic client registration failed ({last})")

    def cimd_document(self) -> dict:
        native = is_loopback_host(urlsplit(self.cfg.redirect_uri).hostname or "")
        return {
            "client_id": self.cfg.cimd_url,
            "client_name": "mcpgw (MCP Gateway)",
            "client_uri": self.cfg.public_url,
            "redirect_uris": [self.cfg.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "application_type": "native" if native else "web",
            "software_id": "mcpgw",
            "software_version": __version__,
        }

    # -- authorization code flow -------------------------------------------

    def _select_scopes(self, up: "Upstream", st: AuthState, step_up: bool) -> list[str]:
        scopes: list[str] = []
        if up.cfg.scopes is not None:
            scopes = list(up.cfg.scopes)
        elif st.challenge_scopes:
            scopes = list(st.challenge_scopes)
        elif st.prm and isinstance(st.prm.get("scopes_supported"), list):
            scopes = [s for s in st.prm["scopes_supported"] if isinstance(s, str)]
        if step_up or st.granted_scopes:
            # step-up: union of previously requested/granted scopes and the new challenge
            for s in st.granted_scopes + st.challenge_scopes:
                if s not in scopes:
                    scopes.append(s)
        as_scopes = (st.as_meta or {}).get("scopes_supported") or []
        if scopes and "offline_access" in as_scopes and "offline_access" not in scopes:
            scopes.append("offline_access")
        return scopes

    async def begin(self, up: "Upstream", *, step_up: bool = False) -> str:
        """Start an authorization code flow; returns the URL to send the browser to."""
        problems = up.problems()
        if problems:
            raise OAuthError(problems[0])
        if up.cfg.auth != "oauth":
            raise OAuthError(f"{up.name} is configured with auth = {up.cfg.auth!r}, not oauth")
        st = await self.discover(up, force=True)
        as_meta = st.as_meta or {}
        scopes = self._select_scopes(up, st, step_up)
        client = await self._client(up, as_meta, scopes)
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        now = time.time()
        for k in [k for k, p in self._pending.items() if now - p.created > PENDING_TTL]:
            del self._pending[k]
        if len(self._pending) > 200:
            self._pending.pop(next(iter(self._pending)))
        self._pending[state] = Pending(
            server=up.name, verifier=verifier, issuer=as_meta["issuer"],
            iss_supported=bool(as_meta.get("authorization_response_iss_parameter_supported")),
            token_endpoint=as_meta["token_endpoint"], resource=st.resource or canonical_resource(up.url),
            scopes=scopes, client=client,
        )
        params = {
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": self.cfg.redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": st.resource or canonical_resource(up.url),
        }
        if scopes:
            params["scope"] = " ".join(scopes)
        params.update(up.provider.authorize_params(up.cfg))
        ep = urlsplit(as_meta["authorization_endpoint"])
        query = "&".join(filter(None, [ep.query, urlencode(params)]))
        return urlunsplit((ep.scheme, ep.netloc, ep.path, query, ""))

    async def complete(self, query: dict[str, str]) -> str:
        """Handle the redirect back from the AS. Returns the server name."""
        state = query.get("state")
        pending = self._pending.pop(state, None) if state else None
        if not pending or time.time() - pending.created > PENDING_TTL:
            raise OAuthError("unknown or expired authorization request; start the connection again")
        # RFC 9207 issuer validation, before acting on code *or* error.
        iss = query.get("iss")
        if iss is not None:
            if iss != pending.issuer:
                raise OAuthError("authorization response issuer mismatch (possible mix-up attack)")
        elif pending.iss_supported:
            raise OAuthError("authorization response is missing the required iss parameter")
        if query.get("error"):
            raise OAuthError(f"authorization failed: {query['error']}"
                             + (f" ({query['error_description']})" if query.get("error_description") else ""))
        code = query.get("code")
        if not code:
            raise OAuthError("authorization response has no code")
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.cfg.redirect_uri,
            "code_verifier": pending.verifier,
            "resource": pending.resource,
        }
        data = await self._token_request(pending.token_endpoint, form, pending.client, pending.server,
                                         pending.issuer)
        await self._save_token(pending.server, data, pending)
        self._signal_connected(pending.server)
        log.info("%s: connected", pending.server)
        return pending.server

    async def _token_request(self, endpoint: str, form: dict, client: dict, server: str,
                             issuer: str) -> dict:
        headers = {"Accept": "application/json"}
        form = dict(form)
        method = client.get("method", "none")
        if method == "client_secret_basic" and client.get("client_secret"):
            cred =f"{quote(client['client_id'], safe='')}:{quote(client['client_secret'], safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        else:
            form["client_id"] = client["client_id"]
            if method == "client_secret_post" and client.get("client_secret"):
                form["client_secret"] = client["client_secret"]
        try:
            async with self.http.post(endpoint, data=form, headers=headers,
                                      timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False) as r:
                text = await r.text()
                ctype = r.headers.get("Content-Type", "")
                status = r.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise OAuthError(f"token endpoint unreachable: {e}") from e
        try:
            data = json.loads(text) if "json" in ctype or text.lstrip().startswith("{") else dict(parse_qsl(text))
        except ValueError:
            data = {}
        if status != 200 or "error" in data or not data.get("access_token"):
            err = data.get("error") or f"http_{status}"
            if err == "invalid_client" and client.get("source") == "dcr":
                await self.store.delete(self.client_key(issuer))
            raise TokenError(err, data.get("error_description") or text[:200])
        tt = str(data.get("token_type", "Bearer"))
        if tt.lower() != "bearer":
            raise OAuthError(f"unsupported token_type {tt!r}")
        return data

    async def _save_token(self, server: str, data: dict, pending: Pending | None = None,
                          previous: dict | None = None) -> dict:
        prev = previous or {}
        scope = data.get("scope")
        scopes = scope.split() if isinstance(scope, str) else (pending.scopes if pending else
                                                                 prev.get("scopes", []))
        rec = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or prev.get("refresh_token"),
            "expires_at": int(time.time() + int(data["expires_in"])) if data.get("expires_in") else None,
            "scopes": scopes,
            "issuer": pending.issuer if pending else prev.get("issuer"),
            "resource": pending.resource if pending else prev.get("resource"),
            "token_endpoint": pending.token_endpoint if pending else prev.get("token_endpoint"),
            "client": _public_client(pending.client) if pending else prev.get("client"),
            "obtained_at": int(time.time()),
        }
        await self.store.put(self.token_key(server), rec)
        st = self.st(server)
        st.granted_scopes = list(scopes)
        st.challenge_scopes = []
        return rec

    async def refresh(self, up: "Upstream", *, stale: str | None, force: bool = False) -> dict | None:
        async with self._lock(up.name):
            tok = await self.token(up)
            if not tok:
                raise AuthRequired(up.name, "not connected")
            if stale and tok.get("access_token") != stale:
                return tok  # refreshed by someone else while we waited for the lock
            exp = tok.get("expires_at")
            if not force and exp and exp - time.time() >= REFRESH_SKEW:
                return tok
            if not tok.get("refresh_token") or not tok.get("token_endpoint"):
                await self.store.delete(self.token_key(up.name))
                raise AuthRequired(up.name, "token expired")
            client = await self._client_for_refresh(up, tok)
            form = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}
            if tok.get("resource"):
                form["resource"] = tok["resource"]
            try:
                data = await self._token_request(tok["token_endpoint"], form, client, up.name,
                                                 tok.get("issuer") or "")
            except TokenError as e:
                if e.error in ("invalid_grant", "invalid_client", "unauthorized_client"):
                    await self.store.delete(self.token_key(up.name))  # terminal: re-authorize
                    raise AuthRequired(up.name, f"refresh rejected ({e.error})") from e
                raise
            log.info("%s: refreshed access token", up.name)
            return await self._save_token(up.name, data, previous=tok)

    async def _client_for_refresh(self, up: "Upstream", tok: dict) -> dict:
        info = tok.get("client") or {}
        if info.get("source") == "config" or (up.cfg.client_id and info.get("client_id") == up.cfg.client_id):
            return {"client_id": up.cfg.client_id, "client_secret": up.cfg.client_secret,
                    "method": info.get("method") or ("client_secret_post" if up.cfg.client_secret else "none"),
                    "source": "config"}
        if info.get("source") == "dcr":
            rec = await self.store.get(self.client_key(tok.get("issuer") or ""))
            if rec:
                return rec
            raise AuthRequired(up.name, "OAuth client registration is gone")
        return {"client_id": info.get("client_id"), "method": "none", "source": info.get("source")}


class TokenError(OAuthError):
    def __init__(self, error: str, description: str):
        super().__init__(f"{error}: {description}")
        self.error = error


def _public_client(client: dict) -> dict:
    """What we keep about the client inside a token record (never the secret)."""
    return {"client_id": client.get("client_id"), "method": client.get("method"), "source": client.get("source")}
