# Testing against real providers

The test suite runs against a strict mock (see [architecture.md](architecture.md#testing-strategy)). This page records what was verified against the **live** hosted servers, the bug that live testing found, and how to repeat the tests yourself.

## Results (2026-10-18)

### Discovery and registration, all six presets

| Step | Notion | Linear | Sentry | Atlassian | Stripe | GitHub |
|---|---|---|---|---|---|---|
| 401 + `WWW-Authenticate` with `resource_metadata` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Protected resource metadata loaded, `resource` matches | ✅ | ✅ | ✅ | ✅ (`/v2/mcp`) | ✅ (root) | ✅ (trailing `/`) |
| AS metadata, exact issuer match | ✅ | ✅ | ✅ | ✅ `auth.atlassian.com/<id>` | ✅ `access.stripe.com/mcp` | fallback ¹ |
| PKCE S256 advertised | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ ¹ |
| `iss` in authorization response advertised | ✅ | ✅ | ✅ | – | – | – |
| CIMD supported | ✅ | ✅ | ✅ | ✅ | – | – |
| Dynamic registration | ✅ | ✅ | ✅ | ✅ | ✅ | ✗ (by design) |
| Valid authorization URL built | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

¹ github.com answered the RFC 8414/OIDC metadata requests with 403 from the test environment, so the GitHub preset's vetted `fallback_as_metadata` was used, as designed. From a normal network the live document may load instead.

Scopes requested when you don't configure any:

| Provider | Default scopes | Source |
|---|---|---|
| Notion | `default` | challenge |
| Linear | `read write` | challenge |
| Sentry | `org:read project:write team:write event:write alerts:write` | PRM `scopes_supported` |
| Atlassian | about 40 scopes, including read/write access to Jira, Confluence, Bitbucket, Loom and more, plus `offline_access` | PRM `scopes_supported` |
| Stripe | `mcp` | PRM `scopes_supported` |
| GitHub | `repo read:org read:user user:email read:packages write:packages read:project project gist notifications` | PRM `scopes_supported` |

The spec says to request the PRM's `scopes_supported` when the challenge names no scope, which is why Atlassian and Sentry ask for so much. Set `scopes = [...]` for those servers if you want least privilege.

### End to end with Notion

| Check | Result |
|---|---|
| Browser sign-in, callback with `iss` validated, code exchanged with PKCE | ✅ |
| Era detection | ✅ modern, **2026-07-28** |
| `tools/list` | ✅ 65 tools, about 260 KB, *after the fix below* |
| `notion-search` via CLI/REST | ✅ |
| Call from a 2026-07-28 client through `/mcp` | ✅ `resultType: complete` |
| Call from a 2025-11-25 client, bridged to the 2026-07-28 upstream | ✅ (no `resultType` leaked) |
| Forced refresh (token marked expired) | ✅ refreshed before use; Notion rotated the refresh token; the next call with the rotated token succeeded |

## Bug found: large responses were truncated (fixed in 0.1.1)

With 0.1.0, Notion's `server/discover` succeeded but `tools/list` failed with `HTTP 200 without a JSON-RPC response`. Notion's reply was valid. It was a single 260 KB JSON object.

The cause was in the gateway. It read response bodies with one call to aiohttp's `StreamReader.read(n)`, which returns only the bytes **already buffered**, not up to `n`. A body that arrives in several TCP chunks was therefore cut short, and the JSON failed to parse. The mock's replies were small enough to arrive in one chunk, so no test caught it. The OAuth metadata fetcher used the same pattern.

The fix is `util.read_body()`, which reads the whole body chunk by chunk and enforces the size limit (32 MB for MCP messages, 256 KB for metadata). The mock can now pad `tools/list` (`big_tools=N`), and `test_large_multi_chunk_response` sends about 400 KB. That test was confirmed to **fail** with the 0.1.0 read code and pass with the fix.

## Repeating the tests yourself

```bash
pip install -e .
cat > mcpgw.toml <<'EOF'
[servers.notion]
provider = "notion"
[servers.linear]
provider = "linear"
readonly = true
EOF
mcpgw -v serve                    # terminal 1
mcpgw login notion                # terminal 2: opens the browser
mcpgw status                      # connected, protocol, tool count
mcpgw call notion__notion-search '{"query": "notes"}'
```

To test the in-protocol connect flow, add the gateway to an MCP client and use a server you haven't connected yet:

```bash
claude mcp add --transport http gateway http://localhost:8765/mcp
```

To force a refresh:

1. Stop the gateway.
2. In `~/.mcpgw/credentials.json`, set `"expires_at": 1` on `token|notion`.
3. Start the gateway and make a call. Look for `refreshed access token` in the log.

| Symptom | Likely cause | Fix |
|---|---|---|
| `does not advertise PKCE ... refusing` | AS metadata omits `code_challenge_methods_supported` | `strict_pkce = false` for that server |
| `rejecting AS metadata ... issuer` | Issuer mismatch, often a trailing slash | Report it; may need a provider fix |
| Provider says `redirect_uri` is invalid | It accepts only `127.0.0.1` or only `localhost` | Set `public_url` accordingly and log in again |
| Atlassian redirect blocked | Org allowlist | Admin allowlists `http://localhost:8765/oauth/callback` |
| GitHub: `requires a pre-registered client_id` | GitHub has no DCR | Create an OAuth App (callback above) or use `auth = "bearer"` with a PAT |

Logs never contain tokens, so `mcpgw -v serve` output is safe to share when reporting a problem.
