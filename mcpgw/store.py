"""Credential storage.

Two kinds of records are kept, both keyed so that the spec's "authorization
server binding" rule holds (credentials are never reused across issuers):

* ``client|<issuer>|<redirect_uri>``  dynamically registered OAuth clients
* ``token|<server>``                  tokens for one upstream server, tagged with
                                      the issuer and resource they were minted for

Swap in another backend (keychain, Vault, a database) by implementing
``TokenStore`` and passing it to ``Gateway(store=...)``.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Any, Protocol


class TokenStore(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...
    async def put(self, key: str, value: dict[str, Any]) -> None: ...
    async def delete(self, key: str) -> None: ...


class MemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    async def get(self, key: str) -> dict[str, Any] | None:
        v = self._data.get(key)
        return dict(v) if v is not None else None

    async def put(self, key: str, value: dict[str, Any]) -> None:
        self._data[key] = dict(value)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)


class FileStore:
    """JSON file with 0600 permissions, written atomically (tmp + rename).

    Tokens are stored in plaintext under those permissions, like most CLI
    credential caches. Use a different backend if that isn't acceptable.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, Any]] | None = None

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._data is None:
            try:
                with open(self.path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except FileNotFoundError:
                self._data = {}
        return self._data

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    async def get(self, key: str) -> dict[str, Any] | None:
        async with self._lock:
            v = self._load().get(key)
            return dict(v) if v is not None else None

    async def put(self, key: str, value: dict[str, Any]) -> None:
        async with self._lock:
            self._load()[key] = dict(value)
            self._flush()

    async def delete(self, key: str) -> None:
        async with self._lock:
            if self._load().pop(key, None) is not None:
                self._flush()


def load_or_create_secret(state_dir: Path) -> bytes:
    """Per-install HMAC key for connect tickets and requestState."""
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state_dir / "secret.key"
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except FileNotFoundError:
        pass
    data = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return data
