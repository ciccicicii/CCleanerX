#!/usr/bin/env python3
"""Small portable secret obfuscation helper for test distribution builds.

This is intentionally a lightweight local encryption layer, not a licensing or
strong secret-protection system. The bundled executable can decrypt it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any


class SecretStoreError(ValueError):
    pass


VERSION = 1
APP_PEPPER = b"LocalDiskCleaner.DeepScan.Bundle.v1"
DEFAULT_PURPOSE = "deepseek-api-key"


def encrypt_secret(secret: str, *, purpose: str = DEFAULT_PURPOSE) -> str:
    if not isinstance(secret, str) or not secret:
        raise SecretStoreError("secret must be a non-empty string")

    salt = secrets.token_bytes(16)
    plaintext = secret.encode("utf-8")
    cipher = xor_bytes(plaintext, keystream(salt, purpose, len(plaintext)))
    tag = sign_payload(salt, purpose, cipher)
    payload = {
        "v": VERSION,
        "salt": b64e(salt),
        "ciphertext": b64e(cipher),
        "tag": b64e(tag),
    }
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")


def decrypt_secret(blob: str, *, purpose: str = DEFAULT_PURPOSE) -> str:
    try:
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
        payload: dict[str, Any] = json.loads(raw.decode("utf-8"))
        if payload.get("v") != VERSION:
            raise SecretStoreError("unsupported secret version")
        salt = b64d(payload["salt"])
        cipher = b64d(payload["ciphertext"])
        tag = b64d(payload["tag"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SecretStoreError("invalid secret blob") from exc

    expected = sign_payload(salt, purpose, cipher)
    if not hmac.compare_digest(tag, expected):
        raise SecretStoreError("secret integrity check failed")

    plaintext = xor_bytes(cipher, keystream(salt, purpose, len(cipher)))
    return plaintext.decode("utf-8")


def load_bundled_secret() -> str | None:
    try:
        from tools import bundled_secrets
    except ImportError:
        return None

    blob = getattr(bundled_secrets, "DEEPSEEK_API_KEY_BLOB", "")
    if not isinstance(blob, str) or not blob.strip():
        return None
    try:
        return decrypt_secret(blob.strip())
    except SecretStoreError:
        return None


def keystream(salt: bytes, purpose: str, length: int) -> bytes:
    output = bytearray()
    counter = 0
    while len(output) < length:
        block = hashlib.sha256(APP_PEPPER + salt + purpose.encode("utf-8") + counter.to_bytes(4, "big")).digest()
        output.extend(block)
        counter += 1
    return bytes(output[:length])


def sign_payload(salt: bytes, purpose: str, cipher: bytes) -> bytes:
    return hmac.new(APP_PEPPER, salt + purpose.encode("utf-8") + cipher, hashlib.sha256).digest()


def xor_bytes(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("ascii"))
