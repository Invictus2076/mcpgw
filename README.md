# mcpgw

[![CI](https://github.com/Invictus2076/mcpgw/actions/workflows/ci.yml/badge.svg)](https://github.com/Invictus2076/mcpgw/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mcpgw-gateway)](https://pypi.org/project/mcpgw-gateway/)
[![Python](https://img.shields.io/pypi/pyversions/mcpgw-gateway)](https://pypi.org/project/mcpgw-gateway/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

A lightweight, pluggable gateway for **Streamable HTTP MCP servers**. It does the OAuth 2.1 dance with each upstream provider, keeps the tokens, and gives your agents **one endpoint** with every tool, prompt and resource behind it.

```
  MCP client ──► http://localhost:8765/mcp ──┬─► GitHub     (pre-registered OAuth App / PAT)
  (any era)       one namespace:             ├─► Notion     (CIMD or DCR, auto)
  REST / CLI      notion__search,            ├─► Linear     (DCR, auto)
                  linear__list_issues, ...   ├─► Sentry / Atlassian / Stripe
                                             └─► any spec-compliant MCP server
```

- **One dependency** (`aiohttp`), Python 3.11+, about 3k lines of code.
- **Spec-current**: speaks MCP **2026-07-28** (stateless, `server/discover`, `Mcp-Method`/`Mcp-Name`/`Mcp-Param-*` headers, MRTR) and falls back to **2025-11-25** sessions. It does this in *both* directions: old and new clients, old and new servers.
- **OAuth 2.1 per the MCP authorization spec**: PRM discovery (RFC 9728), issuer-validated AS metadata (RFC 8414 / OIDC), client registration in spec priority order (pre-registered ▸ CIMD ▸ DCR), PKCE S256, `resource` indicators (RFC 8707), `iss` validation (RFC 9207), refresh with rotation, and step-up on `insufficient_scope`.
- **Pluggable**: providers are small classes; token storage is an interface.

Design details: [docs/architecture.md](docs/architecture.md) · Live provider results: [docs/live-testing.md](docs/live-testing.md) · History: [CHANGELOG.md](CHANGELOG.md)

## Install

```bash
pipx install mcpgw-gateway        # or: pip install mcpgw-gateway
mcpgw --version
```

The PyPI name is `mcpgw-gateway` because `mcpgw` belongs to an unrelated project. The command and the Python package are both `mcpgw`.

Other ways to install:

- **Docker:** `ghcr.io/invictus2076/mcpgw`. See [Docker](#docker).
- **A release file:** download the wheel from [Releases](https://github.com/Invictus2076/mcpgw/releases), then run `pip install mcpgw_gateway-*.whl`.
- **From source:** `git clone https://github.com/Invictus2076/mcpgw && cd mcpgw && pip install -e .`

## Quick start

```bash
mcpgw init notion linear      # writes mcpgw.toml (presets: notion linear sentry atlassian stripe github)
mcpgw doctor                  # checks the config and each server's OAuth setup
mcpgw serve
```

Point your MCP client at `http://localhost:8765/mcp`. For Claude Code:

```bash
claude mcp add --transport http gateway http://localhost:8765/mcp
# with gateway.api_key set:
claude mcp add --transport http gateway http://localhost:8765/mcp --header "Authorization: Bearer $MCPGW_API_KEY"
```

Servers you haven't connected yet show up as a `gateway__connect_<server>` tool. Connect one by:
- calling that tool, or any tool from that server, then following the link; or
- running `mcpgw login notion` (this opens your browser).

The first call to an unconnected server prompts the user in whichever way the client supports:

| Client | What it gets |
|---|---|
| 2026-07-28 with URL elicitation | `InputRequiredResult` → URL elicitation. The retry waits (up to `auth_wait_seconds`) for the browser sign-in, then runs the call. |
| 2025-11-25 with URL elicitation | `URLElicitationRequiredError` (`-32042`) with the connect URL |
| anything else | an `isError` tool result with the link; the user signs in, then retries |
| REST | HTTP 409 `{"error": "connect_required", "connect_url": ...}` |

Prompts and resources can't return a tool result, so they get a JSON-RPC error carrying the link instead (`-32042` for 2025-11-25 clients that support URL elicitation).

## Tools, prompts and resources

| | On `/mcp` | On `/mcp/<server>` |
|---|---|---|
| Tools | `<server>__<tool>`, plus `gateway__status` and `gateway__connect_<server>` | unprefixed |
| Prompts | `<server>__<prompt>` | unprefixed |
| Resources and templates | URIs unchanged; the owner is in `_meta["mcpgw/server"]` | unchanged |

Resource URIs are global identifiers, so the gateway passes them through. To route `resources/read`, it uses exact URIs from listings, the prefix of each URI template, and resource links seen in tool results. If no route is known, it re-lists once before answering "resource not found". Use `tools.allow` / `tools.deny` globs per server to hide tools.

## Built-in providers

| `provider` | Endpoint | Notes |
|---|---|---|
| `github` | `api.githubcopilot.com/mcp/` | GitHub has no dynamic registration. Create an [OAuth App](https://github.com/settings/developers) with callback `http://localhost:8765/oauth/callback` and set `client_id`/`client_secret`, **or** use `auth = "bearer"` with a PAT. Options: `toolsets = [...]`, `readonly = true`. |
| `notion` | `mcp.notion.com/mcp` | Automatic (CIMD/DCR). Refresh tokens rotate; refreshes are serialized. |
| `linear` | `mcp.linear.app/mcp` | `readonly = true` → `/mcp/readonly`. API key works via `auth = "bearer"`. |
| `sentry` | `mcp.sentry.dev/mcp` | `org = "..."`, `project = "..."` for scoping; `experimental = true`. |
| `atlassian` | `mcp.atlassian.com/v2/mcp` | Uses `?tools=all` (flat tool list) by default; `flat_tools = false` to opt out. Org admins may need to allowlist the localhost callback. |
| `stripe` | `mcp.stripe.com` | OAuth; or `auth = "bearer"` with a restricted key. |
| `generic` | `url = "..."` | Anything that follows the MCP spec. |

Endpoints were checked in October 2026, and discovery was verified live against all six (see [docs/live-testing.md](docs/live-testing.md)). Providers move things, and `url = ...` always overrides a preset. If you want least privilege, set `scopes` for Atlassian and Sentry: by default they request every scope they advertise. The full set of options is in [`mcpgw.example.toml`](mcpgw.example.toml).

## CLI

| Command | What |
|---|---|
| `mcpgw init [presets…]` | Write a starter `mcpgw.toml` |
| `mcpgw doctor` | Check the config, the state directory, and each server's OAuth discovery and client registration; then list tools |
| `mcpgw serve` | Run the gateway (stops cleanly on Ctrl-C / SIGTERM) |
| `mcpgw login <server>` · `logout <server>` | Connect a server in the browser · forget its tokens |
| `mcpgw status` · `tools` · `prompts` · `resources` | Show state and listings (`--json` for machine output) |
| `mcpgw call <tool> '<json>'` | Call a tool, e.g. `mcpgw call linear__list_issues '{}'` |
| `mcpgw prompt <prompt> '<json>'` · `read <uri>` | Get a prompt · read a resource |
| `mcpgw providers` | List the built-in presets |

Global options: `-c/--config` (default: `$MCPGW_CONFIG` or `./mcpgw.toml`), `-v`, `--version`. The commands use the running gateway if there is one, and run in-process otherwise.

## Docker

```bash
export MCPGW_API_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
docker run -d --name mcpgw -p 127.0.0.1:8765:8765 -e MCPGW_API_KEY \
  -v mcpgw-data:/data ghcr.io/invictus2076/mcpgw:latest
docker exec mcpgw mcpgw login notion --no-browser
```

Or use `docker compose up -d` with the included [`compose.yaml`](compose.yaml).

Inside a container the gateway listens on `0.0.0.0`, so it requires an API key. Publish the port on loopback, as above, so the OAuth redirect to `http://localhost:8765/oauth/callback` reaches it. The image's [default config](docker/mcpgw.toml) has Notion and Linear. To use your own server list, mount a file over it with `-v ./mcpgw.toml:/etc/mcpgw/mcpgw.toml:ro`. Credentials live in the `/data` volume. The image runs as a non-root user and has a health check.

## HTTP endpoints

| Path | What |
|---|---|
| `POST /mcp` | Aggregated MCP endpoint (tools, prompts, resources) |
| `POST /mcp/<server>` | One upstream, unprefixed names (handy for clients that want a single server) |
| `GET /connect/<server>?t=…` | Confirmation page → redirect to the provider's sign-in |
| `GET /oauth/callback` | OAuth redirect URI |
| `GET /oauth/client-metadata.json` | CIMD document (when `client_metadata_url` / an https `public_url` is set) |
| `GET /api/servers[?probe=1]` | Status, scopes, expiry, connect links |
| `POST /api/servers/<server>/connect` · `/disconnect` | Mint a connect link · forget tokens |
| `GET /api/tools` · `POST /api/tools/call` | `{"name": "linear__list_issues", "arguments": {}}` |
| `GET /api/prompts` · `POST /api/prompts/get` | `{"name": "notion__summarize", "arguments": {"text": "…"}}` |
| `GET /api/resources` · `POST /api/resources/read` | `{"uri": "…"}` |
| `GET /healthz` | Liveness (no auth) |

REST status codes: 200 OK, 400 bad request, 401 missing API key, 404 unknown tool, prompt or resource, 409 `connect_required`, 502 upstream unavailable.

## Writing a provider

```python
from mcpgw.providers import Provider, register

@register
class Acme(Provider):
    name = "acme"
    display_name = "Acme"

    def build_url(self, sc):                  # sc.options holds any extra config keys
        return f"https://mcp.{self.option(sc, 'region', 'us')}.acme.example/mcp"

    def extra_headers(self, sc):              # sent on every MCP request
        return {"X-Acme-Client": "mcpgw"}

    def authorize_params(self, sc):           # extra query params on the authorization URL
        return {"prompt": "select_account"}
```

Load it with `plugins = ["your.module"]` in `[gateway]`, or with a `mcpgw.providers` entry point. A provider whose AS can't do registration sets `needs_client_credentials = True`. It can also ship vetted `fallback_as_metadata`, which is used only when the issuer matches exactly. See `examples/acme_provider.py`.

Storage is the `TokenStore` protocol (`get`/`put`/`delete`). Pass `Gateway(cfg, store=...)` to use a keychain, Vault, or a database instead of the default 0600 JSON file.

## Security model

mcpgw is a **single-user** gateway: whoever can call it acts as you on every connected service.

- It binds to `127.0.0.1` by default and refuses a non-loopback bind without `api_key`.
- The `Origin` header is validated on `/mcp` and `/api`, which covers DNS rebinding.
- The `public_url` must be https unless it's loopback.
- Connect links are HMAC-signed, expire in 15 minutes, and work only once. They open a confirmation page, and only the page's POST starts OAuth, so link unfurlers can't trigger it.
- `requestState` is signed and bound to the tool name and a digest of the arguments.
- No token passthrough: the gateway key never goes upstream, and upstream tokens never go downstream. Tokens are stored per server, tagged with issuer and resource. Registrations are keyed by issuer, and pre-registered client IDs are pinned to theirs.
- All OAuth URLs must be https. Plain http is accepted only when the upstream itself is a local http server, so a remote server can't aim the gateway at your loopback services.
- Tokens live in `~/.mcpgw/credentials.json`, mode 0600, in plaintext, like most CLI credential caches. Swap the store if that's not acceptable.

If you need multiple users, put it behind your own auth and run one gateway per user. Per-user token binding isn't built in. To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Not included (yet)

- **Change notifications:** there are no `subscriptions/listen` or `resources/subscribe` change notifications. Listings are cached for `tool_list_ttl` or the upstream's `ttlMs`.
- **Progress:** responses to clients are plain JSON, so upstream progress notifications are dropped.
- **Tasks and completions:** the Tasks extension and `completion/complete` aren't routed.
- **MRTR on old clients:** a 2025-11-25 client can't answer an upstream's MRTR input requests. The gateway explains this in the tool result.

## Development

```bash
pip install -e ".[test]"                 # Playwright is optional: it enables the real-browser test
python -m unittest discover -s tests -v
```

68 tests run real HTTP between a downstream client, the gateway, and a **strict mock provider**. The mock rejects missing PKCE, a wrong `resource`, unregistered redirects, header/body mismatches, missing sessions, and replayed refresh tokens. One test signs in through the connect page in headless Chromium. Covered paths:

- modern, legacy and dual-era upstreams, and modern and legacy downstreams
- tools, prompts and resources, including cross-server resource routing
- DCR, CIMD and pre-registered (`client_secret_basic`) clients
- OIDC path-appended discovery
- SSE responses with interleaved server pings
- refresh on 401, proactive refresh under concurrency, `invalid_grant` → reconnect
- step-up scope union and `x-mcp-header` mirroring (including base64)
- MRTR passthrough
- `iss` mix-up rejection and AS issuer mismatch
- API key and Origin enforcement
- CLI error handling and `init`

CI runs the suite on Linux, Windows and macOS for Python 3.11–3.13. It also builds the distributions and the Docker image. Releases are automated from version tags: see [docs/releasing.md](docs/releasing.md). Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).
