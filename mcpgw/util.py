from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any



def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    """HMAC-SHA256 signed, expiring blobs (connect tickets, requestState).

    Format: ``<prefix><b64url(json)>.<b64url(mac)>``. The prefix is covered by
    the MAC so blobs minted for one purpose can't be replayed for another.
    """

    def __init__(self, key: bytes) -> None:
        self._key = key

    def _mac(self, prefix: str, body: str) -> str:
        return b64url(hmac.new(self._key, (prefix + body).encode(), hashlib.sha256).digest())

    def sign(self, prefix: str, payload: dict[str, Any], ttl: float) -> str:
        payload = dict(payload, exp=int(time.time() + ttl), n=secrets.token_hex(8))
        body = b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        return f"{prefix}{body}.{self._mac(prefix, body)}"

    def verify(self, prefix: str, blob: str) -> dict[str, Any] | None:
        if not isinstance(blob, str) or not blob.startswith(prefix):
            return None
        body, _, mac = blob[len(prefix):].rpartition(".")
        if not body or not hmac.compare_digest(mac, self._mac(prefix, body)):
            return None
        try:
            payload = json.loads(b64url_decode(body))
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
            return None
        return payload


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
