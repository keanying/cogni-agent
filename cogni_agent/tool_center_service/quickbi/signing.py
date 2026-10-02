"""Quick BI OpenAPI 网关签名(X-Gw-*)。

与官方 skill(scripts/common/utils.py::build_signature)逐字节一致:

    StringToSign = METHOD + "\\n" + URI
                   [+ "\\n" + "k1=v1&k2=v2"(参与签名的参数按 key 升序,空值跳过)]
                   + "\\nX-Gw-AccessId:" + accessId
                   + "\\nX-Gw-Nonce:" + nonce
                   + "\\nX-Gw-Timestamp:" + timestamp
    Signature    = Base64( HMAC-SHA256( api_secret, urlencode(StringToSign, safe="") ) )

注意:参数值**不做** URL 编码后再拼接(原样拼),整串拼好后再整体 quote。
JSON body 不参与签名;GET 的 query 与 x-www-form-urlencoded 表单参与签名。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from typing import Any, Mapping
from urllib import parse


def build_string_to_sign(
    method: str, uri: str, params: Mapping[str, Any] | None,
    access_id: str, nonce: str, timestamp: str,
) -> str:
    query = ""
    if params:
        parts = [f"{k}={params[k]}" for k in sorted(params) if params[k] is not None and params[k] != ""]
        if parts:
            query = "\n" + "&".join(parts)
    headers = (
        "\nX-Gw-AccessId:" + access_id
        + "\nX-Gw-Nonce:" + nonce
        + "\nX-Gw-Timestamp:" + timestamp
    )
    return method.upper() + "\n" + uri + query + headers


def sign(
    method: str, uri: str, params: Mapping[str, Any] | None,
    access_id: str, access_key: str, nonce: str, timestamp: str,
) -> str:
    string_to_sign = build_string_to_sign(method, uri, params, access_id, nonce, timestamp)
    encoded = parse.quote(string_to_sign, "")
    digest = hmac.new(access_key.encode("utf-8"), encoded.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def signed_headers(
    method: str, uri: str, params: Mapping[str, Any] | None,
    *, access_id: str, access_key: str,
    nonce: str | None = None, timestamp: str | None = None,
) -> dict[str, str]:
    nonce = nonce or str(uuid.uuid4())
    timestamp = timestamp or str(int(time.time() * 1000))
    return {
        "X-Gw-AccessId": access_id,
        "X-Gw-Nonce": nonce,
        "X-Gw-Timestamp": timestamp,
        "X-Gw-Signature": sign(method, uri, params, access_id, access_key, nonce, timestamp),
    }
