# Contributing to mcpgw

Thanks for helping. Bug reports, provider presets and fixes are all welcome.

## Development setup

```bash
git clone https://github.com/Invictus2076/mcpgw
cd mcpgw
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[test]"
python -m playwright install chromium               # optional: real-browser sign-in test
```

mcpgw needs Python 3.11 or newer and has one runtime dependency, `aiohttp`.

## Running the tests

```bash
python -m unittest discover -s tests -v
```

The suite runs real HTTP between a downstream client, the gateway and a strict mock provider (`tests/mock_provider.py`), so no network access or accounts are needed. The mock rejects anything the MCP spec forbids, such as missing PKCE, a wrong `resource`, or a header that doesn't match the body. A change that makes the mock more lenient needs a good reason.

The Chromium sign-in test is skipped when Playwright isn't installed. CI always runs it.

## Making a change

- Keep the single runtime dependency. Prefer the standard library.
- Every bug fix comes with a test that fails without the fix.
- Follow the style of the surrounding code: type hints, small functions, comments that explain *why*.
- Add user-visible changes to `CHANGELOG.md` under an `## [Unreleased]` heading.
- Never log tokens, client secrets or the gateway API key.

## Adding a provider preset

Presets live in `mcpgw/providers/builtin.py`. A preset is a small `Provider` subclass. See the "Writing a provider" section of the README and `examples/acme_provider.py`. Before opening the pull request, check the endpoint with `mcpgw doctor`. If you can, describe what you verified against the live server, the way `docs/live-testing.md` does.

## Security issues

Please don't open a public issue. See [SECURITY.md](SECURITY.md).
