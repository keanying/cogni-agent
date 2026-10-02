"""Quick BI 业务错误码 → 可读提示。与官方 skill 的 KNOWN_ERROR_CODES 同口径。"""
from __future__ import annotations

import re

TRIAL_EXPIRED_CODE = "AE0579100004"

KNOWN_ERROR_CODES: dict[str, str] = {
    TRIAL_EXPIRED_CODE: "Quick BI 试用已到期,请在 Quick BI 控制台购买或更换为自有 api_key/api_secret",
    "AE0570010014": "{service} 智能体服务未部署,请联系 Quick BI 管理员开通",
    "AE0580800012": "该功能在当前 Quick BI 版本被裁剪或禁止访问",
    "AE0581030022": "未购买智能小Q(NL2SQL)增值模块",
    "AE0581030029": "当前用户未分配智能小Q席位/配额",
    "AE0581030019": "小Q问数额度已用尽",
    "AE0581030025": "小Q Token 额度已用尽",
    "AE0581030027": "小Q探索版额度已用尽",
    "AE0533330017": "小Q报告免费额度已用尽",
    "AE0533330025": "小Q报告并发数超限,请稍后再试(同一用户同时只能运行一个报告任务)",
    "AE0150100004": "user_token 对应的用户不在 Quick BI 组织内",
    "AE0510200000": "当前用户没有该资源的操作权限(需要数据集问数权限/仪表板查看权限)",
}


class QuickBIError(Exception):
    """调用 Quick BI 失败。

    kind:
      - config    配置缺失/不合法(不应重试)
      - auth      签名/鉴权失败(HTTP 401/403 或网关报错)
      - business  业务错误码(success=false)
      - network   网络/超时(可重试)
      - protocol  返回结构不符合预期
      - input     调用方入参不合法
    """

    def __init__(self, message: str, *, code: str | None = None, kind: str = "business",
                 status: int | None = None, raw: str | None = None):
        super().__init__(message)
        self.code = code
        self.kind = kind
        self.status = status
        self.raw = raw

    @property
    def retryable(self) -> bool:
        return self.kind == "network"

    def to_dict(self) -> dict:
        return {"message": str(self), "code": self.code, "kind": self.kind, "status": self.status}


def _service_name(message: str) -> str:
    if not message:
        return "SmartQ"
    parts = message.split(" agent service")
    return parts[0].strip() if len(parts) > 1 else "SmartQ"


def friendly_message(code: str | None, message: str | None = None) -> str | None:
    """已知错误码返回中文提示;未知返回 None。"""
    if not code or code not in KNOWN_ERROR_CODES:
        return None
    tpl = KNOWN_ERROR_CODES[code]
    return tpl.replace("{service}", _service_name(message or ""))


def scan_known_code(text: str | None) -> str | None:
    """在一段原始文本(错误事件/HTTP body)里扫描已知错误码。"""
    if not text:
        return None
    for code in KNOWN_ERROR_CODES:
        if code in text:
            return code
    m = re.search(r"AE\d{10}", text)
    return m.group(0) if m else None


def describe(code: str | None, message: str | None) -> str:
    friendly = friendly_message(code, message)
    base = message or "Quick BI 调用失败"
    if friendly:
        return f"{friendly}(code={code})" if base == friendly else f"{friendly}(code={code},原始信息:{base})"
    return f"{base}(code={code})" if code else base
