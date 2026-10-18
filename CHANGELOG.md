# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.1] - 2026-10-18

### Fixed
- Large upstream responses were truncated. Bodies were read with a single
  `StreamReader.read(n)`, which returns only what is already buffered, so
  replies arriving in several chunks failed to parse. This broke Notion's
  ~260 KB `tools/list`. Responses and OAuth metadata are now read in full,
  with size limits (`util.read_body`).

### Added
- Regression test with a ~400 KB `tools/list` (`MockProvider(big_tools=N)`);
  41 tests.
- `docs/live-testing.md`: verification results against the live Notion,
  Linear, Sentry, Atlassian, Stripe and GitHub servers, plus a guide to
  repeating them.

### Changed
- README recommends explicit `scopes` for Atlassian and Sentry, which request
  every advertised scope by default.

## [0.1.0] - 2026-10-18

First release.

### Added
- **Aggregating gateway** with one MCP endpoint (`/mcp`) exposing every upstream's tools as `<server>__<tool>`. There's also a scoped endpoint per server (`/mcp/<server>`), and built-in `gateway__status` and `gateway__connect_<server>` tools.
- **Dual-era MCP support in both directions.**
  - MCP 2026-07-28: stateless; `server/discover`; `_meta` request metadata; `Mcp-Method`, `Mcp-Name` and `Mcp-Param-*` headers with base64 sentinel encoding; `x-mcp-header` validation; `resultType`; `ttlMs` and `cacheScope`; MRTR.
  - MCP 2025-11-25 and earlier: `initialize`, `Mcp-Session-Id`, SSE responses with server pings.
- **Upstream era detection** following the spec's backward-compatibility procedure, with session-expiry recovery.
- **OAuth 2.1 client** per the MCP authorization spec:
  - discovery: RFC 9728 protected resource metadata, and RFC 8414 / OIDC authorization server metadata with an exact issuer match;
  - client registration in spec order: pre-registered, then CIMD, then DCR with `application_type`;
  - the authorization flow: PKCE S256 with an enforced support check, RFC 8707 `resource`, `state`, RFC 9207 `iss` validation;
  - tokens: serialized, rotation-safe refresh; terminal handling of `invalid_grant`; step-up on `insufficient_scope` that keeps previously granted scopes.
- **User prompts for authorization:**
  - modern URL elicitation (`InputRequiredResult`), where the gateway holds the retry until sign-in completes;
  - legacy `URLElicitationRequiredError` (`-32042`);
  - a plain link in the tool result;
  - REST `409 connect_required`.
- **Provider presets:** GitHub (OAuth App or PAT, toolsets, read-only), Notion, Linear (read-only endpoint), Sentry (org and project scoping), Atlassian Rovo v2 (flat tool list), Stripe, and `generic`.
- **Plugin system** for providers, via `@register`, `gateway.plugins`, or the `mcpgw.providers` entry point.
- **Pluggable `TokenStore`**, with an atomic, mode-0600 JSON file store and an in-memory store.
- **REST API** (`/api/servers`, `/api/tools`, `/api/tools/call`, connect and disconnect).
- **CLI:** `serve`, `login`, `logout`, `status`, `tools`, `call`, `providers`.
- **Security defaults:** loopback bind, a required API key for a non-loopback bind, Origin validation, single-use signed connect tickets, signed `requestState`, https-only OAuth URLs.
- **Strict mock provider** and 40 end-to-end and unit tests.
