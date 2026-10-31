# Security policy

mcpgw holds OAuth tokens for your accounts, so security reports are taken seriously.

## Reporting a vulnerability

Report it privately through GitHub: **Security → Report a vulnerability** on this repository, or [open a draft advisory](https://github.com/Invictus2076/mcpgw/security/advisories/new). Please don't open a public issue.

Include the version (`mcpgw --version`), what an attacker can do, and the steps to reproduce. Leave out real tokens or credentials. You should get a reply within a week. A fix goes out as a patch release, with credit to you unless you'd rather stay anonymous.

## Supported versions

Security fixes go into the latest minor release.

| Version | Supported |
|---|---|
| 0.2.x | ✅ |
| < 0.2 | ❌ (0.1.x can't complete browser sign-in, among other fixed bugs: upgrade) |

## Scope

These are in scope:
- token or secret leakage: logs, error pages, upstream or downstream requests
- bypassing the API key or Origin checks
- forging or replaying connect links or `requestState`
- OAuth flaws: PKCE, `state`, `iss` validation, issuer and resource binding
- anything that lets a remote MCP server reach the gateway host's loopback services

The threat model is described in the "Security model" section of the README and in [docs/architecture.md](docs/architecture.md#security-model). mcpgw is a single-user gateway: whoever can call it acts as you on every connected service. By design, anyone holding a valid API key has full access.
