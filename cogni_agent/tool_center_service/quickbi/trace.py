"""一次运行的执行留痕:上游原样事件 + 我们归一化之后的事件。

为什么要留原样的:出问题时最缺的就是「Quick BI 到底发了什么」。
归一化之后的事件已经掺进了我们的理解 —— 理解错了,光看它是看不出来的
(这次「思考过程混在回答里」就是典型:归一化后只是一串 text,
看上游原文才知道那是一段 NL2SQL 前言)。

三条纪律:
- **脱敏**:token / 密钥 / 签名一律打码。留痕是要发给人看的,不能带着凭证出门;
- **限长**:单事件截断 + 总量封顶,超了就丢中间段并记「省略了多少条」。
  排障看的是头尾与异常点,不是完整录像;留痕撑爆库比没留痕更糟;
- **坏了就不留**:任何异常都不允许影响这次调用本身。
"""
from __future__ import annotations

import re
from typing import Any

# 事件里可能出现的敏感键(大小写不敏感,子串匹配)
_SECRET_KEYS = ("token", "secret", "password", "signature", "sign", "accesskey", "access_key",
                "apikey", "api_key", "authorization", "cookie", "accessticket", "credential")
# 文本里裸露的凭证形态:accessTicket=xxx / userToken: xxx / xpftkey=xxx 这类。
#
# `[\w-]*key` 这一条是补上去的:原先只写了 apikey|api_key,于是 **xpftkey=** 漏在外面 ——
# 而那正是数据平台接口真实用的 header 名(见 1.17 那份接口文档)。
# 凭证的命名各家各样(appKey / xpftkey / bizKey),枚举不过来,按后缀认才靠得住。
# 宁可多盖一点:导出是要发出去的东西,多盖一个词只是难看,漏一个就是泄漏。
_SECRET_IN_TEXT = re.compile(
    r"((?:token|secret|ticket|signature|password|credential|bearer|[\w-]*key)[\"'\s:=]{1,4})"
    r"([A-Za-z0-9\-_.]{6,})",
    re.I)

MAX_EVENT_CHARS = 2000        # 单个事件序列化后的上限
MAX_EVENTS = 400              # 每一路(raw / normalized)最多留多少条
MAX_HTTP_CALLS = 40           # 非流式请求留多少次
MAX_BODY_CHARS = 6000         # 单次响应原文留多长(选表、仪表板结构这类返回不小)
MAX_TOTAL_CHARS = 256 * 1024  # 整个留痕的总量上限


def mask(value: Any, literals: "frozenset[str] | set[str] | None" = None) -> Any:
    """递归脱敏。

    三道:键名命中敏感词、文本里 `token=xxx` 这类裸露写法、
    以及**已知凭证的字面值**(literals)—— 第三道不能省:
    Quick BI 有些接口把 user_token 放在 `userId` / `oapiUserId` 这种看不出是凭证的字段里,
    只靠键名和正则会漏。
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kl = str(k).lower()
            # prompt_tokens / completion_tokens 是用量计数,不是凭证 —— 以前被「token」一刀切
            # 成 ***,执行日志里「prompt_tokens=***」,恰恰看不到上下文有多大
            if kl.endswith("_tokens") and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = v
            elif any(sk in kl for sk in _SECRET_KEYS):
                out[k] = "***"
            else:
                out[k] = mask(v, literals)
        return out
    if isinstance(value, list):
        return [mask(v, literals) for v in value[:200]]
    if isinstance(value, str):
        s = _SECRET_IN_TEXT.sub(lambda m: m.group(1) + "***", value)
        for lit in literals or ():
            if lit and len(lit) >= 6 and lit in s:
                s = s.replace(lit, "***")
        return s if len(s) <= MAX_EVENT_CHARS else s[:MAX_EVENT_CHARS] + f"…(截断,原长 {len(s)})"
    return value


class RunTrace:
    """一次运行的留痕收集器。两路各自限量,超了只记条数不记内容。"""

    def __init__(self, *, enabled: bool = True, secrets: "list[str] | None" = None):
        self.enabled = enabled
        # 已知凭证的字面值:这一路调用里用到的 user_token / api_secret / api_key。
        # 它们会原样出现在请求参数里(userId=<user_token>),必须按值打码。
        self.secrets = frozenset(x for x in (secrets or []) if x and len(x) >= 6)
        self.raw: list[dict] = []
        self.normalized: list[dict] = []
        self.http: list[dict] = []
        self.dropped_raw = 0
        self.dropped_norm = 0
        self.dropped_http = 0
        self.chars = 0

    def _room(self, text_len: int) -> bool:
        if self.chars + text_len > MAX_TOTAL_CHARS:
            return False
        self.chars += text_len
        return True

    @staticmethod
    def _merge_key(evt: dict) -> tuple | None:
        """能不能跟上一条合并。流式是逐字吐的:一句话能拆成上百个事件,
        原样堆着既撑爆限额、也没人读得下去(一屏全是「用户」「要求解读」「提供的」)。
        同类型同分段的连续增量合并成一条,信息一点不少,可读性天差地别。"""
        t = evt.get("type")
        if t in ("text", "reasoning", "sql", "code"):
            return (t, evt.get("section"), evt.get("channel"))
        return None

    def _append(self, bucket: list[dict], item: dict, key: tuple | None, text_field: str | None) -> bool:
        """合并进上一条则返回 True(不占新名额)。"""
        if key is None or not bucket:
            return False
        prev = bucket[-1]
        if prev.get("_k") != key:
            return False
        a, b = prev.get(text_field) or "", item.get(text_field) or ""
        if len(a) + len(b) > MAX_EVENT_CHARS:
            return False
        prev[text_field] = a + b
        prev["_merged"] = prev.get("_merged", 1) + 1
        return True

    def add_raw(self, uri: str, evt: dict) -> None:
        if not self.enabled:
            return
        masked = mask(evt, self.secrets)
        # 上游原样事件按 type+subType 合并(data 是逐字增量)
        key = None
        if isinstance(masked.get("data"), str) and masked.get("type") in ("text", "summary", "reasoning", "sql"):
            key = ("raw", uri, masked.get("type"), masked.get("subType"))
        if self.raw and key and self.raw[-1].get("_k") == key:
            prev = self.raw[-1]["event"]
            if len(prev.get("data") or "") + len(masked.get("data") or "") <= MAX_EVENT_CHARS:
                prev["data"] = (prev.get("data") or "") + (masked.get("data") or "")
                self.raw[-1]["_merged"] = self.raw[-1].get("_merged", 1) + 1
                return
        if len(self.raw) >= MAX_EVENTS:
            self.dropped_raw += 1
            return
        item = {"uri": uri, "event": masked}
        if key:
            item["_k"] = key
        if not self._room(len(str(item))):
            self.dropped_raw += 1
            return
        self.raw.append(item)

    def add_normalized(self, evt: dict) -> None:
        if not self.enabled:
            return
        # 归一化事件本身不含凭证,但 delta 可能很长,照样截断
        item = mask({k: v for k, v in evt.items() if k != "_sseId"}, self.secrets)
        key = self._merge_key(item)
        if self._append(self.normalized, item, key, "delta"):
            return
        if len(self.normalized) >= MAX_EVENTS:
            self.dropped_norm += 1
            return
        if key:
            item["_k"] = key
        if not self._room(len(str(item))):
            self.dropped_norm += 1
            return
        self.normalized.append(item)

    def add_http(self, method: str, uri: str, req: Any, resp_text: str, status: int) -> None:
        """非流式请求的原始一问一答。选表、仪表板结构、快照、报告进度都在这儿 ——
        「API 第一版输出到底长什么样」只有这里能回答。"""
        if not self.enabled:
            return
        if len(self.http) >= MAX_HTTP_CALLS:
            self.dropped_http += 1
            return
        body = resp_text or ""
        truncated = len(body) > MAX_BODY_CHARS
        item = {
            "method": method, "uri": uri, "status": status,
            "request": mask(req, self.secrets) if req is not None else None,
            "response": mask(body[:MAX_BODY_CHARS], self.secrets)
            + (f"…(截断,原长 {len(body)})" if truncated else ""),
        }
        if not self._room(len(str(item))):
            self.dropped_http += 1
            return
        self.http.append(item)

    @staticmethod
    def _clean(items: list[dict]) -> list[dict]:
        """去掉合并用的内部键;合并过的标上「由几条增量合并而来」,别让人以为丢了事件。"""
        out = []
        for it in items:
            c = {k: v for k, v in it.items() if k != "_k"}
            n = c.pop("_merged", None)
            if n:
                c["_merged_deltas"] = n
            out.append(c)
        return out

    def to_dict(self) -> dict | None:
        if not self.enabled or not (self.raw or self.normalized or self.http):
            return None
        return {
            "http": self.http,
            "raw": self._clean(self.raw),
            "normalized": self._clean(self.normalized),
            "dropped": {"raw": self.dropped_raw, "normalized": self.dropped_norm,
                        "http": self.dropped_http},
            "limits": {"max_events": MAX_EVENTS, "max_event_chars": MAX_EVENT_CHARS,
                       "max_http_calls": MAX_HTTP_CALLS, "max_body_chars": MAX_BODY_CHARS,
                       "max_total_chars": MAX_TOTAL_CHARS},
        }
