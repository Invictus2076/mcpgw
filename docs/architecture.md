# Architecture

This document explains how mcpgw is put together, why each piece exists, and which parts of the MCP specification it implements. It targets MCP **2026-07-28**, and it stays compatible with **2025-11-25** and earlier on both the client and the server side.

## Overview

```
              downstream (your agents)                          upstream (hosted MCP servers)
 ┌────────────────────────────────┐                  ┌──────────────────────────────────────────┐
 │ MCP client, 2026-07-28 or      │   POST /mcp      │ mcpgw                                    │
 │ 2025-11-25 (Claude Code, ...)  ├─────────────────►│  server.py    HTTP surface, era handling │
 │ REST caller / mcpgw CLI        │   /api/*         │  gateway.py   namespacing, routing,       │
 └────────────────────────────────┘                  │               auth prompts               │
                                                     │  upstream.py  one client per server ─────┼──► GitHub, Notion, Linear,
 ┌────────────────────────────────┐   /connect/...   │  oauth.py     OAuth 2.1 client           │    Sentry, Atlassian, Stripe,
 │ user's browser                 ├─────────────────►│  providers/   per-service presets        │    any spec-compliant server
 └────────────────────────────────┘   /oauth/callback│  store.py     token storage              │
                                                     └──────────────────────────────────────────┘
```

| Module | Responsibility |
|---|---|
| `protocol.py` | Version constants, `_meta` keys, error codes, `Mcp-Name`/`Mcp-Param-*` value encoding, `x-mcp-header` validation |
| `config.py` | TOML loading with `${ENV}` / `${ENV:-default}` interpolation, validation and safety checks |
| `providers/` | `Provider` base class, registry, plugin loading, and the built-in presets |
| `store.py` | `TokenStore` protocol, `FileStore` (atomic, mode 0600) and `MemoryStore`, plus the per-install HMAC secret |
| `oauth.py` | `AuthManager`: discovery, client registration, the authorization code flow, refresh, and step-up |
| `upstream.py` | `Upstream`: Streamable HTTP client for one server, with era detection, SSE parsing and tool caching |
| `gateway.py` | `Gateway`: aggregation, name resolution, connect links, auth prompts, MRTR passthrough |
| `server.py` | aiohttp app: the dual-era `/mcp` endpoint, browser pages, REST API, security middleware |
| `__main__.py` | CLI |

## Protocol eras

The 2026-07-28 revision made MCP stateless. It removed the `initialize` handshake, `Mcp-Session-Id`, the GET stream and server-to-client requests on SSE. In their place, every request carries `_meta` (protocol version, client info, client capabilities) and mirrored HTTP headers. In October 2026 most hosted servers had not finished migrating, so mcpgw handles both eras on both sides.

### Upstream: detecting what each server speaks

`Upstream.ensure_ready()` follows the backward-compatibility procedure from the Streamable HTTP spec:

1. Send a **modern** `server/discover` request with `_meta`, `MCP-Protocol-Version: 2026-07-28` and `Mcp-Method`.
2. If the result has `supportedVersions` that include 2026-07-28, the server is modern. Its capabilities, `serverInfo` and instructions are recorded.
3. If the body is a recognized modern error (`-32020`, `-32021` or `-32022`), the server is modern, but the request needs fixing. For `UnsupportedProtocolVersion`, the client picks from `data.supported`.
4. Anything else is a **legacy** server: a 4xx without a modern error body, `-32601`, or junk. The client then sends `initialize` (offering 2025-11-25), keeps `Mcp-Session-Id`, sends `notifications/initialized`, and sends `MCP-Protocol-Version` on every later request.

The era is cached per server. A legacy `404` on a request that carried a session means the session expired, so the client re-initializes and retries once. A modern server answering `-32022` causes a reset and a re-probe.

Modern requests always carry `Mcp-Method`. Requests for `tools/call`, `prompts/get` and `resources/read` also carry `Mcp-Name`. For tools whose `inputSchema` has `x-mcp-header` annotations, the client also sends `Mcp-Param-{Name}` headers. Values that aren't safe as plain ASCII use the `=?base64?…?=` sentinel. If a server rejects a call with `HeaderMismatch`, the tool list is refreshed once and the call retried, as the spec recommends.

Tool definitions with invalid `x-mcp-header` annotations are dropped, as the spec requires. That covers annotations on non-primitive types, duplicates (case-insensitive), names that aren't field-name tokens, and annotations not reachable through a chain of `properties` keys.

Responses may be JSON or SSE. The SSE reader handles CRLF, comments, multi-line `data:` fields and events split across chunks. It caps message size, ignores progress notifications, and answers a legacy server's `ping` requests on the stream. The gateway declares no client capabilities to legacy servers, so `ping` is the only request a compliant server can send.

### Downstream: serving both kinds of client

`server.py` classifies each POST:

| Request shape | Handling |
|---|---|
| `method == "initialize"` without `_meta` | Legacy: mint a session (`Mcp-Session-Id`), negotiate a legacy version, store client capabilities |
| `_meta["io.modelcontextprotocol/protocolVersion"]` present | Modern: validate headers against the body, then serve statelessly |
| `Mcp-Session-Id` present | Legacy: look up the session (404 if unknown), check `MCP-Protocol-Version` |
| Neither | 400 |

Modern header validation returns HTTP 400 with `HeaderMismatch` (`-32020`) when `MCP-Protocol-Version`, `Mcp-Method` or `Mcp-Name` is missing or doesn't match the body. Mcp-Name values are base64-decoded first. An unknown version gets `UnsupportedProtocolVersion` (`-32022`) with the supported list. Unknown methods return 404 with `-32601` for modern clients and 200 with `-32601` for legacy ones. GET returns 405, and DELETE ends a legacy session.

Modern results gain `resultType: "complete"` and `_meta.serverInfo`. `tools/list` results also gain `ttlMs` and `cacheScope: "private"`. Legacy results have `resultType` removed. Legacy sessions expire after 24 hours idle and are capped at 1000.

The gateway strips `x-mcp-header` from the schemas it exposes. Downstream clients therefore never send `Mcp-Param-*` headers to it, and the gateway computes them itself for each upstream.

## Naming and routing

Tools are exposed as `<server><separator><tool>`, with `__` as the default separator. Server names are limited to `[a-z0-9-]`, so they can never contain the separator, and splitting on the first separator always gives the right pair. `gateway` is reserved for built-ins:

- `gateway__status` lists every server with its state and a connect link where one is needed.
- `gateway__connect_<server>` appears only while that server needs authorization.

`/mcp/<server>` is a scoped endpoint. It exposes one upstream's tools without the prefix, plus `mcpgw_connect` while that server isn't connected.

`tools/list` fans out to all upstreams concurrently, with a timeout for each. A failing or unauthorized server never hides the others' tools. Each upstream's list is cached for its own `ttlMs`, or for `tool_list_ttl` when the server doesn't send one. The aggregated `ttlMs` is the smallest remaining lifetime, capped at 30 seconds while any server still needs authorization.

## Authorization (upstream OAuth 2.1)

`AuthManager` implements the MCP client side of the authorization spec.

### Discovery

1. **Challenge.** A 401 or 403 is parsed with a full RFC 7235 `WWW-Authenticate` parser, which handles multiple challenges, quoted strings and token68. The parser records `resource_metadata` and `scope`.
2. **Protected Resource Metadata (RFC 9728).** The client tries the `resource_metadata` URL first. Then it tries `/.well-known/oauth-protected-resource/<path>`, then the root well-known URL. The document's `resource` must have the same origin as the server URL and a matching path prefix, and it becomes the `resource` parameter.
3. **Authorization server metadata.** For an issuer with a path, the client tries RFC 8414 with path insertion, then OIDC with path insertion, then OIDC with the path appended. For an issuer without a path, it tries RFC 8414, then OIDC. The document's `issuer` must equal the issuer **exactly**, or the document is rejected.
4. **PKCE check.** `code_challenge_methods_supported` must contain `S256`. If the field is missing, the client refuses, as the spec requires, unless `strict_pkce = false`.
5. **Authorization server binding.** If a stored token came from a different issuer than the one now advertised, the token is discarded.

A provider can ship `fallback_as_metadata`, such as GitHub's endpoints. It is used only for that exact issuer: discovered fields take priority, and the fallback fills gaps or stands in when discovery fails.

### Client registration

The spec's priority order is followed:

1. **Pre-registered** `client_id`/`client_secret` from config. The token endpoint auth method is configurable, or the best one the AS supports is picked. These credentials are pinned to an issuer, either the `issuer` option or the provider's vetted issuer, and are never sent to a different AS.
2. **Client ID Metadata Document**, when the AS advertises `client_id_metadata_document_supported` and the gateway has an https URL for its CIMD (`client_metadata_url`, or an https `public_url`). The gateway serves the document at `/oauth/client-metadata.json`.
3. **Dynamic Client Registration** (RFC 7591), the deprecated fallback. The request includes `application_type` (`native` for a loopback redirect, `web` otherwise, with a retry using the other value on rejection) and `token_endpoint_auth_method: none`. Registrations are stored under `client|<issuer>|<redirect_uri>`.

### Authorization code flow

The request uses a fresh PKCE S256 verifier, a random `state`, the `resource`, and scopes chosen in this order:

1. explicit config `scopes`;
2. otherwise the challenge `scope`;
3. otherwise the PRM `scopes_supported`;
4. otherwise none.

During step-up, previously granted scopes are added, and `offline_access` is added when the AS lists it. The expected issuer is recorded with the pending request. Pending requests expire after 10 minutes.

The callback validates `state`. It then validates `iss` per RFC 9207, following the spec's table, **before** acting on either `code` or `error`. Exchanging the code sends `code_verifier`, `redirect_uri` and `resource`, with client auth set to `none`, `client_secret_post` or `client_secret_basic`. JSON and form-encoded token responses are both accepted, since GitHub returns form encoding unless asked for JSON.

### Tokens, refresh and step-up

- Token records hold the issuer, resource, token endpoint, granted scopes, expiry and public client info, but never the client secret.
- A token is refreshed **proactively** when it expires within 60 seconds, and **reactively** on a 401 that carried the current token.
- Refreshes are serialized per server with a lock. A waiter that finds the token already refreshed uses the new one. This prevents replaying a rotated-out refresh token, which some providers, Notion included, punish by revoking the whole grant.
- `invalid_grant`, `invalid_client` and `unauthorized_client` are terminal: the token is deleted and the user must reconnect.
- A 403 with `error="insufficient_scope"` raises a **step-up** `AuthRequired`. The next authorization requests the union of the granted scopes and the challenge scopes.
- `AuthManager` keeps a per-server **generation counter**. A retried call waits until an authorization newer than the one current at prompt time completes, which is what makes step-up work while an older token still exists.

## Prompting the user to connect

When an upstream raises `AuthRequired`, `Gateway._auth_prompt` picks the best channel the client supports:

| Client | Response |
|---|---|
| Modern, `elicitation.url` capability | `InputRequiredResult` with a URL-mode `elicitation/create` and a signed `requestState` |
| Legacy, `elicitation.url` capability | JSON-RPC error `-32042` (`URLElicitationRequiredError`) with `elicitationId` |
| Other MCP clients | `isError` tool result containing the link (plus `_meta["mcpgw/connectUrl"]`) |
| REST | HTTP 409 `connect_required` |

A modern client then retries with `inputResponses` and the echoed `requestState`. The gateway verifies the state, which is HMAC-signed, expires after 15 minutes, and is bound to the tool name and a digest of the arguments. A `decline` or `cancel` becomes a tool error. Otherwise the gateway waits up to `auth_wait_seconds` for the browser sign-in and then runs the original call.

Connect links (`/connect/<server>?t=…`) carry a signed, single-use ticket that expires after 15 minutes. GET only shows a confirmation page. POST, with an Origin check, consumes the ticket and redirects to the provider. Link previewers and prefetchers therefore can't start a flow, and a used link can't be replayed.

## MRTR passthrough

When a modern upstream returns `InputRequiredResult` (for example a form elicitation) and the downstream client is also modern, the result is passed through. Its `requestState` is prefixed with `up.` so the gateway can tell it apart from its own `gw1.` states. On retry the prefix is stripped, and `inputResponses` and `requestState` go to the upstream unchanged. The downstream client's `elicitation`, `sampling` and `roots` capabilities are forwarded on each modern request, which the stateless protocol allows. A legacy downstream client can't take part in MRTR, so it gets a tool error explaining why.

## Extension points

- **Providers.** Subclass `Provider` and override `build_url`, `extra_headers`, `authorize_params` and `problems`. You can also set `needs_client_credentials` and `fallback_as_metadata`. Register a provider with `@register`, with `gateway.plugins`, or with a `mcpgw.providers` entry point.
- **Token storage.** Implement `TokenStore` (`get`/`put`/`delete`) and pass it as `Gateway(cfg, store=...)`.
- **Programmatic use.** Build the app with `server.create_app(Gateway(cfg))`, or use `Gateway.list_tools()` and `Gateway.call_tool()` directly.

## Security model

mcpgw is designed as a **single-user** gateway.

| Threat | Mitigation |
|---|---|
| Other machines calling the gateway | Binds to `127.0.0.1` by default; refuses a non-loopback bind without `api_key`; constant-time key comparison |
| DNS rebinding / malicious web pages | `Origin` validated on `/mcp` and `/api`; connect POST checks Origin |
| Link prefetch / CSRF starting OAuth | Single-use, expiring, HMAC-signed tickets; confirmation page; POST-only start |
| Authorization code interception | PKCE S256 always; `state` checked; pending requests expire |
| AS mix-up | RFC 9207 `iss` validation; exact issuer match on metadata; credentials keyed by issuer |
| Token passthrough / confused deputy | The gateway key is never sent upstream; upstream tokens are never returned downstream; `resource` binds tokens to one server |
| SSRF via attacker-supplied metadata URLs | All OAuth URLs must be https; plain http only on loopback, and only when the upstream itself is local; redirects not followed; size limits on bodies |
| Forged `requestState` | HMAC-signed, expiring, bound to tool name and argument digest |
| Token theft at rest | Credentials file mode 0600 in a 0700 directory; pluggable store for keychain/Vault |

Not covered: multi-user isolation (run one gateway per user behind your own auth), and encryption at rest with the default store.

## Testing strategy

`tests/mock_provider.py` is a strict, in-process authorization server plus MCP server, configurable as modern, legacy or dual-era. It **rejects** any request that breaks the spec:

- missing PKCE;
- a wrong `resource`;
- unregistered redirect URIs;
- header/body mismatches;
- a missing `Mcp-Param-*` header;
- missing sessions;
- reused authorization codes;
- replayed refresh tokens;
- the wrong client authentication method.

A passing test therefore shows the gateway is compliant, not just that it completes the happy path. `tests/test_gateway.py` runs real HTTP between a downstream client, the gateway and the mock. `tests/test_units.py` covers the parsers and encoders against the examples in the spec.
