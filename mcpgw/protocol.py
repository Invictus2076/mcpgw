"""MCP protocol constants and small helpers shared by the client and server sides.

The gateway is "dual-era" in both directions:

* modern  = 2026-07-28 and later: stateless, every request carries ``_meta``
            (protocol version, client info, client capabilities) plus mirrored
            HTTP headers (``Mcp-Method``, ``Mcp-Name``, ``Mcp-Param-*``).
* legacy  = 2025-11-25 and earlier: ``initialize`` handshake, ``Mcp-Session-Id``.
"""

from __future__ import annotations

import base64
import re
from typing import Any

from . import __version__

MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
ALL_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS
LATEST_MODERN = MODERN_VERSIONS[0]
LATEST_LEGACY = LEGACY_VERSIONS[0]

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# JSON-RPC / MCP error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
MISSING_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022
URL_ELICITATION_REQUIRED = -32042  # legacy (2025-11-25) only

# Error codes that identify a server as "modern" during era detection.
MODERN_ERROR_CODES = {HEADER_MISMATCH, MISSING_CLIENT_CAPABILITY, UNSUPPORTED_PROTOCOL_VERSION}

# Methods whose target name is mirrored into the Mcp-Name header.
NAME_HEADER_METHODS = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}

CLIENT_INFO = {"name": "mcpgw", "version": __version__}
SERVER_INFO = {"name": "mcpgw", "title": "MCP Gateway", "version": __version__}


class RpcError(Exception):
    """A JSON-RPC error to return to (or received from) a peer."""

    def __init__(self, code: int, message: str, data: Any = None, http_status: int = 200):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
        self.http_status = http_status

    def to_dict(self) -> dict:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


def rpc_result(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def rpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    msg: dict[str, Any] = {"jsonrpc": "2.0", "error": err}
    if req_id is not None:
        msg["id"] = req_id
    return msg


# ---------------------------------------------------------------------------
# Header value encoding (Streamable HTTP, 2026-07-28 "Value Encoding")
# ---------------------------------------------------------------------------

_SENTINEL = re.compile(r"^=\?base64\?.*\?=$", re.S)


def encode_header_value(value: str) -> str:
    """Encode a value for Mcp-Name / Mcp-Param-* headers.

    Plain if every char is visible ASCII / space / tab, with no leading or
    trailing whitespace and not itself looking like the sentinel; otherwise
    ``=?base64?<b64 of utf-8>?=``.
    """
    safe = (
        value != ""
        and all(c == "\t" or 0x20 <= ord(c) <= 0x7E for c in value)
        and value == value.strip(" \t")
        and not _SENTINEL.match(value)
    )
    if safe:
        return value
    return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def decode_header_value(value: str) -> str:
    if _SENTINEL.match(value):
        return base64.b64decode(value[len("=?base64?") : -2]).decode("utf-8")
    return value


# ---------------------------------------------------------------------------
# x-mcp-header validation / extraction
# ---------------------------------------------------------------------------

_TCHAR = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_ALLOWED_HEADER_TYPES = {"string", "integer", "boolean"}
# Keys whose values are instance data, not subschemas.
_INSTANCE_KEYS = {"x-mcp-header", "default", "const", "enum", "examples"}
_JS_SAFE = 2**53 - 1


def collect_param_headers(schema: Any) -> list[tuple[str, tuple[str, ...]]]:
    """Return [(header_name, property_path)] for a tool's inputSchema.

    Raises ValueError if any ``x-mcp-header`` annotation violates the spec's
    constraints (in which case the whole tool must be dropped).
    """
    found: list[tuple[str, tuple[str, ...]]] = []
    seen: set[str] = set()

    def walk(node: Any, path: tuple[str, ...] | None) -> None:
        # path is the properties-only chain from the root, or None once we've
        # left it (items, oneOf, $ref, ...). The root itself is not a property.
        if isinstance(node, list):
            for item in node:
                walk(item, None)
            return
        if not isinstance(node, dict):
            return
        if "x-mcp-header" in node:
            name = node["x-mcp-header"]
            if path is None or len(path) == 0:
                raise ValueError("x-mcp-header on a property that is not statically reachable")
            if not isinstance(name, str) or not name or not _TCHAR.match(name):
                raise ValueError(f"invalid x-mcp-header value {name!r}")
            if name.lower() in seen:
                raise ValueError(f"duplicate x-mcp-header {name!r}")
            typ = node.get("type")
            types = set(typ) if isinstance(typ, list) else {typ}
            if not (types - {"null"}) or not (types - {"null"}) <= _ALLOWED_HEADER_TYPES:
                raise ValueError(f"x-mcp-header {name!r} on non-primitive type {typ!r}")
            seen.add(name.lower())
            found.append((name, path))
        for key, child in node.items():
            if key == "properties" and isinstance(child, dict):
                for prop, sub in child.items():
                    walk(sub, (path + (prop,)) if path is not None else None)
            elif key not in _INSTANCE_KEYS:
                walk(child, None)

    walk(schema, ())
    return found


def param_header_values(spec: list[tuple[str, tuple[str, ...]]], arguments: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, path in spec:
        cur = arguments
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                cur = None
                break
            cur = cur[key]
        if cur is None:
            continue
        if isinstance(cur, bool):
            text = "true" if cur else "false"
        elif isinstance(cur, int):
            if abs(cur) > _JS_SAFE:
                continue
            text = str(cur)
        elif isinstance(cur, float) and cur.is_integer():
            text = str(int(cur))
        elif isinstance(cur, str):
            text = cur
        else:
            continue
        headers[f"Mcp-Param-{name}"] = encode_header_value(text)
    return headers


def strip_param_headers(schema: Any) -> Any:
    """Deep copy of a schema with every x-mcp-header annotation removed."""
    if isinstance(schema, dict):
        return {k: strip_param_headers(v) for k, v in schema.items() if k != "x-mcp-header"}
    if isinstance(schema, list):
        return [strip_param_headers(v) for v in schema]
    return schema
