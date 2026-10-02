"""G-API-04:API Key 生成/校验/吊销——存哈希,首次创建时返回明文一次。"""
from __future__ import annotations

import hashlib
import secrets


def generate_api_key() -> tuple[str, str, str]:
    """返回 (明文 key, 哈希值, 展示用前缀)。"""
    raw = f"sk-zhilv-{secrets.token_hex(16)}"
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    prefix_display = raw[:14] + "..." + raw[-4:]
    return raw, key_hash, prefix_display


def verify_api_key(raw: str, key_hash: str) -> bool:
    return hashlib.sha256(raw.encode()).hexdigest() == key_hash
