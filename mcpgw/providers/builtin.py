"""Built-in presets for well-known hosted MCP servers (endpoints checked Oct 2026)."""

from __future__ import annotations

from urllib.parse import quote

from ..config import ConfigError, ServerConfig
from . import Provider, register


def _flag(v) -> bool:
    return v is True or (isinstance(v, str) and v.lower() in ("1", "true", "yes"))


@register
class GitHub(Provider):
    """GitHub's remote MCP server.

    GitHub's OAuth server has no dynamic client registration, so bring your own
    OAuth App / GitHub App (client_id + client_secret) or a personal access token.
    """

    name = "github"
    display_name = "GitHub"
    default_url = "https://api.githubcopilot.com/mcp/"
    docs = "https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md"
    needs_client_credentials = True
    fallback_as_metadata = {
        "issuer": "https://github.com/login/oauth",
        "authorization_endpoint": "https://github.com/login/oauth/authorize",
        "token_endpoint": "https://github.com/login/oauth/access_token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
    }

    def extra_headers(self, sc: ServerConfig) -> dict[str, str]:
        h: dict[str, str] = {}
        toolsets = self.option(sc, "toolsets")
        if toolsets:
            if isinstance(toolsets, str):
                toolsets = [toolsets]
            h["X-MCP-Toolsets"] = ",".join(toolsets)
        if _flag(self.option(sc, "readonly", False)):
            h["X-MCP-Readonly"] = "true"
        return h

    def problems(self, sc: ServerConfig, redirect_uri: str) -> list[str]:
        if sc.auth == "oauth" and not sc.client_id:
            return [
                "GitHub's authorization server does not support dynamic client registration. "
                f"Create a GitHub OAuth App with callback URL {redirect_uri} and set "
                "client_id / client_secret, or use auth = \"bearer\" with a personal access token."
            ]
        return super().problems(sc, redirect_uri)


@register
class Notion(Provider):
    name = "notion"
    display_name = "Notion"
    default_url = "https://mcp.notion.com/mcp"
    docs = "https://developers.notion.com/guides/mcp/build-mcp-client"


@register
class Linear(Provider):
    name = "linear"
    display_name = "Linear"
    docs = "https://linear.app/docs/mcp"

    def build_url(self, sc: ServerConfig) -> str:
        return "https://mcp.linear.app/mcp" + ("/readonly" if _flag(self.option(sc, "readonly")) else "")


@register
class Sentry(Provider):
    name = "sentry"
    display_name = "Sentry"
    docs = "https://mcp.sentry.dev"

    def build_url(self, sc: ServerConfig) -> str:
        url = "https://mcp.sentry.dev/mcp"
        org, project = self.option(sc, "org"), self.option(sc, "project")
        if project and not org:
            raise ConfigError(f"servers.{sc.name}: 'project' needs 'org'")
        if org:
            url += "/" + quote(str(org), safe="")
            if project:
                url += "/" + quote(str(project), safe="")
        if _flag(self.option(sc, "experimental")):
            url += "?experimental=1"
        return url


@register
class Atlassian(Provider):
    """Atlassian Rovo MCP (v2). ``flat_tools`` (default on) asks for the full tool
    list instead of the discover/execute meta-tools, which suits an aggregator."""

    name = "atlassian"
    display_name = "Atlassian"
    docs = "https://support.atlassian.com/atlassian-ai-gateway/docs/how-to-upgrade-from-atlassian-rovo-mcp-v1-to-atlassian-rovo-mcp-v2/"

    def build_url(self, sc: ServerConfig) -> str:
        url = "https://mcp.atlassian.com/v2/mcp"
        return url + ("?tools=all" if _flag(self.option(sc, "flat_tools", True)) else "")


@register
class Stripe(Provider):
    name = "stripe"
    display_name = "Stripe"
    default_url = "https://mcp.stripe.com"
    docs = "https://docs.stripe.com/mcp"
