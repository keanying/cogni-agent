"""Quick BI OpenAPI 异步客户端(httpx)。

只做「协议」这一层:签名、发请求、解信封、SSE 分帧。业务编排在 capabilities.py。
不读数据库、不读全局配置 —— 调用方构造 ClientConfig 传进来,测试可注入 transport。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Mapping

import httpx

from cogni_agent.tool_center_service.quickbi.errors import QuickBIError, describe, scan_known_code
from cogni_agent.tool_center_service.quickbi.signing import signed_headers

DEFAULT_SERVER_DOMAIN = "https://bi.aliyun.com"
_UNSET: Any = object()

# 测试钩子:未显式传 transport 时使用的 transport 工厂(离线测试指向模拟服务)。生产环境保持 None。
default_transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None


@dataclass
class ClientConfig:
    server_domain: str = DEFAULT_SERVER_DOMAIN
    api_key: str = ""
    api_secret: str = ""
    user_token: str = ""
    locale: str = "zh_CN"
    timeout: float = 60.0            # 普通接口
    stream_timeout: float = 600.0    # SSE 流(问数/解读最长可达数分钟)
    verify_ssl: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def base(self) -> str:
        return (self.server_domain or DEFAULT_SERVER_DOMAIN).rstrip("/")

    def missing(self) -> list[str]:
        return [k for k in ("server_domain", "api_key", "api_secret", "user_token") if not getattr(self, k)]

    def validate(self) -> None:
        miss = self.missing()
        if miss:
            hint = ""
            if "api_key" in miss or "api_secret" in miss:
                hint = (";api_key/api_secret 请在 Quick BI 控制台 → 右上角头像 →「一键复制 skill 配置」获取,"
                        "或配置环境变量 QUICKBI_TRIAL_API_KEY / QUICKBI_TRIAL_API_SECRET 使用试用凭证")
            raise QuickBIError(f"Quick BI 连接配置不完整,缺少:{', '.join(miss)}{hint}", kind="config")
        if not self.base.startswith(("http://", "https://")):
            raise QuickBIError(f"server_domain 必须以 http(s):// 开头:{self.server_domain}", kind="config")

    def masked(self) -> dict:
        def _m(v: str) -> str:
            return "" if not v else (v[:4] + "****" + v[-4:] if len(v) > 10 else "****")
        return {"server_domain": self.base, "api_key": _m(self.api_key), "api_secret": "****" if self.api_secret else "",
                "user_token": _m(self.user_token), "locale": self.locale}


def is_success(body: Any) -> bool:
    if not isinstance(body, dict) or "success" not in body:
        return True
    val = body.get("success")
    return val is True or str(val).lower() == "true"


def unwrap(body: Any, *, uri: str = "") -> Any:
    """OpenAPI 统一信封 {success, code, message, data} → data;success=false 抛业务错误。"""
    if isinstance(body, dict) and "success" in body:
        if not is_success(body):
            code = str(body.get("code") or body.get("errorCode") or "") or None
            message = str(body.get("message") or body.get("errorMsg") or "")
            raise QuickBIError(describe(code, message or f"{uri} 调用失败"), code=code, kind="business",
                               raw=json.dumps(body, ensure_ascii=False)[:2000])
        data = body.get("data")
        if isinstance(data, str):
            text = data.strip()
            if text[:1] in ("{", "["):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return data
        return data
    return body


def parse_sse_block(block: str) -> dict | None:
    """解析一个 SSE 事件块,返回 data 里的 JSON 对象。

    Quick BI 事件形如:
        event:message
        data:{"data":"xxx","type":"reasoning"}
    """
    data_lines = [ln[5:] for ln in block.splitlines() if ln.startswith("data:")]
    if not data_lines:
        return None
    data_lines = [s[1:] if s.startswith(" ") else s for s in data_lines]  # SSE 规范:只去掉冒号后一个空格
    raw = "\n".join(data_lines).strip()
    if not raw:
        return None
    for candidate in (raw, data_lines[0].strip()):
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else {"type": "raw", "data": obj}
        except json.JSONDecodeError:
            continue
    try:
        obj = json.loads(raw.replace('\\"', '"').replace("\\\\", "\\"))
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    return {"type": "raw", "data": raw}


class QuickBIClient:
    """用法:

        async with QuickBIClient(cfg) as qbi:
            data = await qbi.get("/openapi/v2/smartq/query/llmCubeWithThemeList", params={...})
            async for evt in qbi.stream("/openapi/v2/smartq/queryByQuestionStream", {...}):
                ...
    """

    def __init__(self, cfg: ClientConfig, *, transport: httpx.AsyncBaseTransport | None = None,
                 on_raw: "Callable[[str, dict], None] | None" = None,
                 on_http: "Callable[[str, str, Any, str, int], None] | None" = None):
        self.cfg = cfg
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        # 上游原始 SSE 事件旁路(排障留痕用);None = 不留
        self.on_raw = on_raw
        # 非流式请求的**原始响应**旁路:选表、仪表板结构、快照、报告进度都走这条,
        # 它们才是「API 第一版输出」。只有 SSE 那一路的话,这些接口返回了什么无从查起。
        self.on_http = on_http

    async def __aenter__(self) -> "QuickBIClient":
        self.cfg.validate()
        transport = self._transport
        if transport is None and default_transport_factory is not None:
            transport = default_transport_factory()
        self._client = httpx.AsyncClient(
            base_url=self.cfg.base, transport=transport, verify=self.cfg.verify_ssl,
            timeout=httpx.Timeout(self.cfg.timeout, connect=15.0),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("QuickBIClient 需在 async with 中使用")
        return self._client

    @property
    def user_id(self) -> str:
        return self.cfg.user_token

    def _headers(self, method: str, uri: str, sign_params: Mapping[str, Any] | None,
                 content_type: str | None = None) -> dict[str, str]:
        headers = signed_headers(method, uri, sign_params, access_id=self.cfg.api_key, access_key=self.cfg.api_secret)
        headers["X-Gw-Debug"] = "true"
        headers["origin"] = self.cfg.base
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    @staticmethod
    def _raise_http(resp: httpx.Response, method: str, uri: str, body_text: str) -> None:
        code = scan_known_code(body_text)
        message = body_text[:500]
        try:
            j = json.loads(body_text)
            if isinstance(j, dict):
                code = str(j.get("code") or j.get("errorCode") or code or "") or None
                message = str(j.get("message") or j.get("errorMsg") or message)
        except (json.JSONDecodeError, ValueError):
            pass
        kind = "auth" if resp.status_code in (401, 403) else ("network" if resp.status_code >= 500 else "business")
        raise QuickBIError(
            describe(code, f"HTTP {resp.status_code} {method} {uri}:{message}"),
            code=code, kind=kind, status=resp.status_code, raw=body_text[:2000],
        )

    async def request(
        self, method: str, uri: str, *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        form: Mapping[str, Any] | None = None,
        files: Mapping[str, Any] | None = None,
        sign_params: Any = _UNSET,
        timeout: float | None = None,
        raw: bool = False,
    ) -> Any:
        """发起非流式请求。raw=True 返回原始文本,否则解析 JSON 并解信封。"""
        method = method.upper()
        if sign_params is _UNSET:
            sign_params = params if method == "GET" else (form if (form is not None and files is None) else None)
        content_type = None
        if json_body is not None:
            content_type = "application/json"
        elif form is not None and files is None:
            content_type = "application/x-www-form-urlencoded"
        headers = self._headers(method, uri, sign_params, content_type)
        kwargs: dict[str, Any] = {"headers": headers}
        if timeout:
            kwargs["timeout"] = timeout
        if params is not None:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        if files is not None:
            kwargs["files"] = files
            if form:
                kwargs["data"] = dict(form)
        elif json_body is not None:
            kwargs["content"] = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        elif form is not None:
            kwargs["data"] = dict(form)
        try:
            resp = await self.http.request(method, uri, **kwargs)
        except httpx.TimeoutException as exc:
            raise QuickBIError(f"请求 Quick BI 超时:{method} {uri}", kind="network") from exc
        except httpx.ProxyError as exc:
            raise QuickBIError(f"出网代理拒绝连接 {self.cfg.base}({exc}),请检查服务器出网白名单是否放行该域名",
                               kind="network") from exc
        except httpx.HTTPError as exc:
            raise QuickBIError(f"无法连接 Quick BI({self.cfg.base}):{exc}", kind="network") from exc
        text = resp.text
        # 先留痕再判状态码:出错的那一次响应恰恰是最需要看原文的
        if self.on_http is not None:
            try:
                self.on_http(method, uri, params if params is not None else (json_body or form),
                             text, resp.status_code)
            except Exception:  # noqa: BLE001 - 留痕是附加品,坏了也不能影响这次调用
                self.on_http = None
        if resp.status_code >= 300:
            self._raise_http(resp, method, uri, text)
        if raw:
            return text
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            return text
        return unwrap(body, uri=uri)

    async def get(self, uri: str, params: Mapping[str, Any] | None = None, **kw) -> Any:
        return await self.request("GET", uri, params=params, **kw)

    async def post_json(self, uri: str, body: Any, **kw) -> Any:
        return await self.request("POST", uri, json_body=body, **kw)

    def _tap(self, uri: str, evt: dict) -> None:
        if self.on_raw is None:
            return
        try:
            self.on_raw(uri, evt)
        except Exception:  # noqa: BLE001 - 留痕是附加品,坏了也不能影响这次调用
            self.on_raw = None

    async def stream(self, uri: str, body: Any, *, timeout: float | None = None) -> AsyncIterator[dict]:
        """POST 流式请求,逐个 yield 解析后的 SSE data 对象。

        挂了 `on_raw` 时,每个事件在归一化之前先原样旁路一份 —— 排障时最缺的就是
        「上游到底发了什么」:归一化之后的事件已经是我们的理解,理解错了就看不出来了。
        旁路失败绝不影响主流程。
        """
        headers = self._headers("POST", uri, None, "application/json")
        headers.update({"Accept": "text/event-stream", "Accept-Encoding": "identity", "Cache-Control": "no-cache"})
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        to = httpx.Timeout(timeout or self.cfg.stream_timeout, connect=15.0)
        try:
            async with self.http.stream("POST", uri, content=payload, headers=headers, timeout=to) as resp:
                if resp.status_code >= 300:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    self._raise_http(resp, "POST", uri, text)
                ctype = resp.headers.get("content-type", "")
                if "event-stream" not in ctype and "json" in ctype:
                    # 网关在业务失败时直接回 JSON 信封而不是 SSE
                    text = (await resp.aread()).decode("utf-8", "replace")
                    try:
                        unwrap(json.loads(text), uri=uri)
                    except json.JSONDecodeError:
                        pass
                    else:
                        raise QuickBIError(f"{uri} 未返回事件流:{text[:300]}", kind="protocol", raw=text[:2000])
                buffer = ""
                async for chunk in resp.aiter_text():
                    buffer += chunk.replace("\r\n", "\n").replace("\r", "\n")
                    while "\n\n" in buffer:
                        block, buffer = buffer.split("\n\n", 1)
                        if block.strip():
                            evt = parse_sse_block(block)
                            if evt is not None:
                                self._tap(uri, evt)
                                yield evt
                if buffer.strip():
                    evt = parse_sse_block(buffer)
                    if evt is not None:
                        self._tap(uri, evt)
                        yield evt
        except httpx.TimeoutException as exc:
            raise QuickBIError(f"Quick BI 流式接口超时:{uri}", kind="network") from exc
        except httpx.ProxyError as exc:
            raise QuickBIError(f"出网代理拒绝连接 {self.cfg.base}({exc}),请检查服务器出网白名单是否放行该域名",
                               kind="network") from exc
        except httpx.HTTPError as exc:
            raise QuickBIError(f"Quick BI 流式连接中断:{uri}:{exc}", kind="network") from exc
