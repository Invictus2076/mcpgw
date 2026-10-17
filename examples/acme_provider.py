"""Example provider plugin.

Load it with ``plugins = ["examples.acme_provider"]`` (module must be importable),
or ship it in a package with an entry point:

    [project.entry-points."mcpgw.providers"]
    acme = "acme_mcpgw:Acme"

then configure:

    [servers.acme]
    provider = "acme"
    region = "eu"
"""

from mcpgw.config import ConfigError, ServerConfig
from mcpgw.providers import Provider, register


@register
class Acme(Provider):
    name = "acme"
    display_name = "Acme"

    def build_url(self, sc: ServerConfig) -> str:
        region = self.option(sc, "region", "us")
        if region not in ("us", "eu"):
            raise ConfigError(f"servers.{sc.name}.region must be 'us' or 'eu'")
        return f"https://mcp.{region}.acme.example/mcp"

    def extra_headers(self, sc: ServerConfig) -> dict[str, str]:
        return {"X-Acme-Client": "mcpgw"}

    def authorize_params(self, sc: ServerConfig) -> dict[str, str]:
        # e.g. force the account chooser on every connect
        return {"prompt": "select_account"}
