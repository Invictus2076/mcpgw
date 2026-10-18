# mcpgw

A lightweight, pluggable gateway for **Streamable HTTP MCP servers**. It does the OAuth 2.1 dance with each upstream provider, keeps the tokens, and gives your agents **one endpoint** with every tool behind it.

```
  MCP client ──► http://localhost:8765/mcp ──┬─► GitHub     (pre-registered OAuth App / PAT)
  (any era)       one tool namespace:        ├─► Notion     (CIMD or DCR, auto)
  REST / CLI      notion__search,            ├─► Linear     (DCR, auto)
                  linear__list_issues, ...   ├─► Sentry / Atlassian / Stripe
                                             └─► any spec-compliant MCP server
```

- **One dependency** (`aiohttp`), Python 3.11+, ~2.5k lines of code.
- **Spec-current**: speaks MCP **2026-07-28** (stateless, `server/discover`, `Mcp-Method`/`Mcp-Name`/`Mcp-Param-*` headers, MRTR) and falls back to **2025-11-25** sessions. It does this in *both* directions: old and new clients, old and new servers.
- **OAuth 2.1 per the MCP authorization spec**: PRM discovery (RFC 9728), issuer-validated AS metadata (RFC 8414 / OIDC), client registration in spec priority order (pre-registered ▸ CIMD ▸ DCR), PKCE S256, `resource` indicators (RFC 8707), `iss` validation (RFC 9207), refresh with rotation, and step-up on `insufficient_scope`.
- **Pluggable**: providers are small classes; token storage is an interface.

Design details: [docs/architecture.md](docs/architecture.md) · Live provider results: [docs/live-testing.md](docs/live-testing.md) · History: [CHANGELOG.md](CHANGELOG.md)

## Quick start

```bash
pip install -e .
cp mcpgw.example.toml mcpgw.toml     # keep the servers you want
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

Endpoints were checked in October 2026, and discovery was verified live against all six (see [docs/live-testing.md](docs/live-testing.md)). Providers move things, and `url = ...` always overrides a preset. If you want least privilege, set `scopes` for Atlassian and Sentry: by default they request every scope they advertise.

## Endpoints

| Path | What |
|---|---|
| `POST /mcp` | Aggregated MCP endpoint: `<server>__<tool>` names, plus `gateway__status`, `gateway__connect_<server>` |
| `POST /mcp/<server>` | One upstream, unprefixed names (handy for clients that want a single server) |
| `GET /connect/<server>?t=…` | Confirmation page → redirect to the provider's sign-in |
| `GET /oauth/callback` | OAuth redirect URI |
| `GET /oauth/client-metadata.json` | CIMD document (when `client_metadata_url` / an https `public_url` is set) |
| `GET /api/servers[?probe=1]` | Status, scopes, expiry, connect links |
| `POST /api/servers/<server>/connect` · `/disconnect` | Mint a connect link · forget tokens |
| `GET /api/tools` · `POST /api/tools/call` | `{"name": "linear__list_issues", "arguments": {}}` |
| `GET /healthz` | Liveness (no auth) |

CLI: `mcpgw serve | login <server> | logout <server> | status | tools | call <tool> '<json>' | providers`. The commands talk to the running gateway if there is one, and run in-process otherwise.

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

If you need multiple users, put it behind your own auth and run one gateway per user. Per-user token binding isn't built in.

## Not included (yet)

Tools only: there's no resources or prompts aggregation, and no `subscriptions/listen` change notifications. Responses to clients are plain JSON, so upstream progress notifications are dropped. The Tasks extension isn't routed. A 2025-11-25 client can't answer an upstream's MRTR input requests (the gateway explains this in the tool result).

## Tests

```bash
python -m unittest discover -s tests -v
```

41 tests run real HTTP between a downstream client, the gateway, and a **strict mock provider**. The mock rejects missing PKCE, a wrong `resource`, unregistered redirects, header/body mismatches, missing sessions, and replayed refresh tokens. Covered paths:

- modern/legacy/dual-era upstreams, and modern/legacy downstreams
- DCR, CIMD and pre-registered (`client_secret_basic`) clients
- OIDC path-appended discovery
- SSE responses with interleaved server pings
- refresh on 401, proactive refresh under concurrency, `invalid_grant` → reconnect
- step-up scope union and `x-mcp-header` mirroring (including base64)
- MRTR passthrough
- `iss` mix-up rejection and AS issuer mismatch
- API key / Origin enforcement
