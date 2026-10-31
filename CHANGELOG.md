# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-01

### Fixed
- **Browser sign-in failed in real browsers.** The connect page set
  `referrer` to `no-referrer`. Under the Fetch spec that makes browsers send
  `Origin: null` on the page's form POST, which the gateway's CSRF check
  rejected ("Not allowed"). Connect links and `mcpgw login` therefore never
  completed in Chrome, Edge or Firefox. The tests missed it because their
  simulated browser sent a normal Origin. The page now uses `same-origin`,
  which still keeps the ticket out of the Referer sent to the provider. A new
  Chromium test drives the real flow.
- Connect links and `requestState` containing non-ASCII characters crashed
  HMAC verification (`TypeError`) and returned HTTP 500; they are now
  rejected normally.
- A dynamic client registration endpoint that was unreachable or answered
  with non-JSON produced an unhandled 500 in the middle of sign-in. It now
  shows an error page saying what went wrong.
- `expires_in` sent as a decimal string (e.g. `"3599.5"`) crashed token saving.
- `mcpgw login` against a running gateway reported "connected" at once when
  the server was already connected, before the new sign-in finished. It now
  waits for the sign-in it started.
- `mcpgw logout <unknown>` printed a traceback when no gateway was running.
  A wrong API key against a running gateway gave a `KeyError` traceback.
  Both now print a one-line error.
- Invalid config values (`port = "abc"`, a non-table `tools` or `headers`,
  malformed TOML) raised raw Python errors; they are now config errors that
  name the bad key.
- A corrupt `credentials.json` crashed every request with a JSON error. It is
  now reported clearly, and the file is left untouched.
- The REST API answered 404 for invalid tool arguments; it now returns 400.
  It returns 404 only for unknown tools, prompts and resources.

### Added
- **Prompts and resources aggregation.** The gateway now serves
  `prompts/list`, `prompts/get`, `resources/list`,
  `resources/templates/list` and `resources/read`, in both protocol eras and
  on scoped endpoints.
  - Prompts are namespaced like tools (`<server>__<prompt>`).
  - Resource URIs pass through unchanged. Reads are routed by exact URI,
    template prefix, or links seen in tool results, with the owning server
    recorded in `_meta["mcpgw/server"]`.
  - When a server isn't connected, the error carries a connect link (or
    `-32042` URL elicitation for 2025-11-25 clients).
- REST endpoints: `GET /api/prompts`, `POST /api/prompts/get`,
  `GET /api/resources`, `POST /api/resources/read`.
- CLI:
  - `mcpgw init` writes a starter config.
  - `mcpgw doctor` checks the config, the state directory and each server's
    OAuth discovery and client registration, then lists tools.
  - `mcpgw prompts`, `mcpgw prompt`, `mcpgw resources` and `mcpgw read` use
    the new prompt and resource support.
  - `mcpgw --version`.
- `mcpgw serve` shuts down gracefully on SIGTERM (`docker stop`).
- Distribution:
  - published on PyPI as **`mcpgw-gateway`** (the command is still `mcpgw`);
  - a container image `ghcr.io/invictus2076/mcpgw` with `compose.yaml`;
  - GitHub Actions for CI (Linux, Windows and macOS; Python 3.11–3.13;
    Chromium; packaging; Docker) and for releases (GitHub release, PyPI
    trusted publishing, multi-arch image).
- Project files: `CONTRIBUTING.md`, `SECURITY.md`, `docs/releasing.md`, issue
  and pull request templates, Dependabot, and the `py.typed` marker.
- `/api/servers` rows include `auth_generation`.

### Changed
- The server's advertised capabilities now include `prompts` and `resources`
  (`listChanged: false`, `subscribe: false`).
- Tests: 68 (from 41).

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
