# -*- coding: utf-8 -*-
"""数据指标接口文档 → 30 个 HTTP 工具,以及它们共同依赖的「景区」怎么来。

两件事,但缠在一起:这 30 个接口**全都依赖景区** —— 20 个直接要 scenicId,
10 个要 supplierId,而 supplierId 是按景区换出来的。所以同步工具只是一半,
另一半是「景区到底从哪来、没有的时候怎么办」。

钉住的几件事:

1. **密钥不落库。** 文档每个 curl 示例里都写着真实 xpftkey,而旧的导入器会把 curl 里的
   请求头原样存进工具配置、再把整份文档原文存进 api_doc_markdown —— 导入一次,
   密钥就以明文进了库。
2. **建出来的工具必须能调。** 文档里有 5 个 curl 没加引号,旧解析器只认带引号的,
   于是这 5 个会被建成「没有域名、没有认证头」—— 列表里看着好好的,一调就失败。
   其中一个恰好是「按景区取供应商 ID」,它一坏,另外 10 个接口跟着全坏。
3. **景区:指定 > 当前归属 > 澄清。** 原先集团账号发起请求时 supplierId 被静默跳过,
   注释写着「由路由层向用户确认」,但没有任何代码去确认。
"""
from __future__ import annotations

import asyncio
import functools
import importlib.util
import json
import re
from pathlib import Path

import pytest

from cogni_agent.common import db as common_db
from cogni_agent.common import schema as _all_models  # noqa: F401
from cogni_agent.common.db import Base
from cogni_agent.common.errors import ClarificationNeeded
from cogni_agent.tool_center_service.apidoc import (
    ApiDocParseError, annotate_params, from_markdown, is_credential_header, placeholder_for,
)

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "tests" / "fixtures" / "data_api_doc.md"

# fixture 里把真实 key 换成了同形状的假 key(真 key 永远不进仓库)。
# 测试断言的是这把假 key「不在任何输出里」—— 形状一样,脱敏逻辑认得出它。
KEY_PREFIX = "pfk_FAKEtestKEY"


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


def _script():
    spec = importlib.util.spec_from_file_location("sync_data_api_tools",
                                                  ROOT / "scripts" / "sync_data_api_tools.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def doc_md() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def chunks(doc_md):
    return _script().split_doc(doc_md)


# ══════════════════════════════════════════════════════════════════════
# 一、文档 → 工具定义
# ══════════════════════════════════════════════════════════════════════

class TestSplitAndParse:
    def test_thirty_endpoints(self, chunks):
        """文档编号到 1.31,但 1.17 是个空标题 —— 实际 30 个。"""
        assert len(chunks) == 30

    def test_every_endpoint_parses(self, chunks):
        for c in chunks:
            from_markdown(c)

    def test_names_are_unescaped(self, chunks):
        """导出的标题是「1\\.1 票务\\-今日客流统计」;不先还原转义,工具名就是「\\.1 票务\\-…」。"""
        names = [from_markdown(c).title for c in chunks]
        assert names[0] == "票务-今日客流统计"
        assert not any("\\" in n for n in names)
        assert not any(re.match(r"^[.\d]", n) for n in names), "编号没去干净"

    def test_param_names_have_no_html_leftovers(self, chunks):
        """表格里的换行被导出成 `<br>`,不去掉的话参数就叫 `startTime<br>`,
        接口收到一个它不认识的字段,真正的 startTime 缺失,回一句「参数错误」。"""
        for c in chunks:
            props = from_markdown(c).to_type_config()["params_schema"].get("properties") or {}
            for name in props:
                assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name), f"参数名不干净:{name!r}"

    def test_every_tool_has_an_absolute_url_and_auth_header(self, chunks):
        """这是「建得出来但调不通」的那 5 个:curl 没加引号,旧解析器认不出地址和请求头。"""
        for c in chunks:
            tc = from_markdown(c).to_type_config()
            assert tc["url"].startswith("https://"), f"{tc['url']} 不是完整地址"
            assert tc["headers"].get("xpftkey") == "{{data_platform_key}}", tc["headers"]

    def test_supplier_lookup_specifically_is_callable(self, chunks):
        """它一坏,另外 10 个要 supplierId 的接口跟着全坏 —— 单独钉一条。"""
        c = next(c for c in chunks if "getSupplierIdByScenicId" in c)
        tc = from_markdown(c).to_type_config()
        assert tc["url"].endswith("/getSupplierIdByScenicId")
        assert tc["url"].startswith("https://")
        assert "xpftkey" in tc["headers"]


class TestSecretsNeverStored:
    def test_no_plaintext_key_anywhere_in_any_tool(self, chunks):
        for c in chunks:
            d = from_markdown(c)
            blob = json.dumps(d.to_type_config(), ensure_ascii=False) + json.dumps(d.to_dict(), ensure_ascii=False)
            assert KEY_PREFIX not in blob, f"「{d.title}」里还有明文密钥"

    def test_stored_doc_text_is_scrubbed_too(self, chunks):
        """api_doc_markdown 存的是整份原文 —— curl 里的密钥会跟着一起进库。"""
        for c in chunks:
            assert KEY_PREFIX not in from_markdown(c).to_type_config()["api_doc_markdown"]

    def test_the_warning_tells_where_the_real_value_goes(self, chunks):
        w = " ".join(from_markdown(chunks[0]).warnings)
        assert "{{data_platform_key}}" in w and "tool_center.secrets" in w

    @pytest.mark.parametrize("name,expect", [
        ("xpftkey", True), ("X-Api-Key", True), ("Authorization", True), ("appKey", True),
        ("access_token", True), ("Content-Type", False), ("Accept", False), ("X-Trace-Id", False),
    ])
    def test_credential_header_recognition(self, name, expect):
        assert is_credential_header(name) is expect

    def test_platform_key_maps_to_a_server_side_secret_not_a_personal_credential(self):
        """{{credential}} 是按「用户 × 工具」存的个人凭证;平台共用 key 放那里,
        等于每个用户要对每个工具各录一遍。"""
        assert placeholder_for("xpftkey") == "{{data_platform_key}}"
        assert placeholder_for("Authorization") == "{{credential}}"

    def test_a_generic_doc_with_a_bearer_token_is_redacted(self):
        """不止这份文档 —— 后台「按接口文档新建」对任何文档都该这样。"""
        md = ("# 1.1 查订单\n### 1.1.1 API概览\nURI:/v1/orders\n请求方式:POST\n"
              "### 1.1.4 示例\n```bash\ncurl --location --request POST 'https://x.example.com/v1/orders' "
              "--header 'Authorization: Bearer sk-live-ABCDEF1234567890' --data-raw '{}'\n```\n")
        d = from_markdown(md)
        assert d.headers["Authorization"] == "{{credential}}"
        assert "sk-live-ABCDEF1234567890" not in d.markdown
        assert "sk-live-ABCDEF1234567890" not in json.dumps(d.to_type_config(), ensure_ascii=False)

    def test_already_placeholdered_headers_are_left_alone(self):
        md = ("# 1.1 x\n### 概览\nURI:/a\n### 示例\n```bash\ncurl --request POST 'https://h.com/a' "
              "--header 'xpftkey: {{data_platform_key}}'\n```\n")
        assert from_markdown(md).headers["xpftkey"] == "{{data_platform_key}}"


class TestAnnotations:
    def test_only_x_keys_are_accepted(self):
        """这个入口是给「系统怎么补参」的,不能顺手改掉参数类型或必填。"""
        tc = {"params_schema": {"properties": {"a": {"type": "string"}}}}
        with pytest.raises(ApiDocParseError):
            annotate_params(tc, {"a": {"type": "integer"}})

    def test_unknown_param_is_an_error_not_silently_ignored(self):
        """写错参数名的声明等于没写,而那个参数会照旧交给模型去编。"""
        tc = {"params_schema": {"properties": {"supplierId": {"type": "array"}}}}
        with pytest.raises(ApiDocParseError):
            annotate_params(tc, {"supplierID": {"x-ctx-default": "$ctx.scenicId"}})

    def test_merges_without_losing_the_original_schema(self):
        tc = {"params_schema": {"properties": {"scenicId": {"type": "string", "description": "景区ID"}},
                                "required": ["scenicId"]}}
        out = annotate_params(tc, {"scenicId": {"x-ctx-default": "$ctx.scenicId"}})
        p = out["params_schema"]["properties"]["scenicId"]
        assert p["type"] == "string" and p["description"] == "景区ID"
        assert p["x-ctx-default"] == "$ctx.scenicId"
        assert out["params_schema"]["required"] == ["scenicId"]
        assert "x-ctx-default" not in tc["params_schema"]["properties"]["scenicId"], "不能改到入参"

    def test_server_and_script_share_one_implementation(self):
        """两份实现迟早对不上,表现是「脚本预览的和实际建出来的不一样」。"""
        src = (ROOT / "cogni_agent" / "tool_center_service" / "routes.py").read_text(encoding="utf-8")
        assert "from cogni_agent.tool_center_service.apidoc import ApiDocParseError, annotate_params" in src
        script = (ROOT / "scripts" / "sync_data_api_tools.py").read_text(encoding="utf-8")
        assert "annotate_params" in script


# ══════════════════════════════════════════════════════════════════════
# 二、同步脚本
# ══════════════════════════════════════════════════════════════════════

class TestSyncScript:
    def test_script_never_contains_a_key_or_default_password(self):
        src = (ROOT / "scripts" / "sync_data_api_tools.py").read_text(encoding="utf-8")
        assert KEY_PREFIX not in src
        for weak in ("0000@123", "pftroot123"):
            assert weak not in src, "脚本里不写任何默认密码"

    def test_scrub_runs_before_anything_leaves_the_machine(self, doc_md):
        s = _script()
        for item in s.build_items(doc_md, base_url=None):
            assert KEY_PREFIX not in item["markdown"], "上送的原文里还有密钥"

    def test_supplier_id_is_system_filled_by_the_builtin(self):
        """v3.52 起 supplierId 由内置 sys_supplier_ids 补齐,不再指向工具库里的某条工具
        (那条要绑、要发布、要可见,任一环断了票务类就全部调不通)。"""
        s = _script()
        ann = s.annotations_for(["supplierId", "startTime"], is_supplier=False,
                                supplier_tool_name="通过景区ID获取供应商ID列表")
        src = ann["supplierId"]["x-param-source"]
        assert src == {"tool": "sys_supplier_ids", "key": "$ctx.scenicId", "path": "supplierId"}
        assert "startTime" not in ann, "时间参数由模型按用户的话填,不做系统补齐"

    def test_scenic_id_gets_a_context_default(self):
        ann = _script().annotations_for(["scenicId", "timeGran"], is_supplier=False,
                                        supplier_tool_name="x")
        assert ann == {"scenicId": {"x-ctx-default": "$ctx.scenicId"}}

    def test_supplier_tool_does_not_depend_on_itself(self):
        ann = _script().annotations_for(["scenicId"], is_supplier=True, supplier_tool_name="self")
        assert "supplierId" not in ann

    def test_every_one_of_the_thirty_gets_a_scenic_source(self, doc_md):
        """30 个接口全都依赖景区 —— 没有哪一个该把景区交给模型去编。"""
        s = _script()
        for item in s.build_items(doc_md, base_url=None):
            ann = s.annotations_for(item["props"], is_supplier=item["is_supplier"],
                                    supplier_tool_name="sup")
            assert ann, f"「{item['name']}」没有任何景区来源声明"

    def test_existing_tools_are_matched_by_uri_not_name(self):
        s = _script()
        existing = [{"id": "t1", "name": "通过景区ID获取供应商ID列表", "type": "http",
                     "type_config": {"method": "POST",
                                     "url": "https://data.12301dev.com/v1/data/digitalBrain/getSupplierIdByScenicId"}}]
        idx = s.index_existing(existing)
        assert ("POST", "/v1/data/digitalBrain/getSupplierIdByScenicId") in idx

    def test_host_change_does_not_create_a_duplicate(self):
        """--base-url 换了域名,路径还是同一个接口 —— 不能因为域名不同再建一份。"""
        s = _script()
        idx = s.index_existing([{"id": "t1", "name": "x", "type": "http",
                                 "type_config": {"method": "POST", "url": "https://old.host/v1/a"}}])
        assert ("POST", "/v1/a") in idx

    def test_plaintext_key_detection_for_existing_tools(self):
        s = _script()
        assert s.plaintext_key_in({"type_config": {"headers": {"xpftkey": "pfk_realvalue123456789"}}})
        assert not s.plaintext_key_in({"type_config": {"headers": {"xpftkey": "{{data_platform_key}}"}}})

    def test_base_url_override(self, doc_md):
        items = _script().build_items(doc_md, base_url="https://data.prod.example")
        assert all(i["url"].startswith("https://data.prod.example/") for i in items)

    def test_default_is_preview_only(self):
        src = (ROOT / "scripts" / "sync_data_api_tools.py").read_text(encoding="utf-8")
        assert 'add_argument("--apply", action="store_true"' in src
        assert "if not args.apply:" in src

    def test_refuses_to_apply_without_the_server_side_key(self):
        """没配密钥就换占位符,已经能用的工具会立刻变成 401。"""
        src = (ROOT / "scripts" / "sync_data_api_tools.py").read_text(encoding="utf-8")
        assert "拒绝写入" in src and "secrets_configured" in src


# ══════════════════════════════════════════════════════════════════════
# 三、执行时:密钥注入、供应商取值、景区缺省与澄清
# ══════════════════════════════════════════════════════════════════════

class TestSecretInjection:
    @staticmethod
    def _inject(headers, secrets, monkeypatch):
        from cogni_agent.tool_center_service.sandbox import local

        class _S:
            def get(self, *path, default=None):
                if path == ("tool_center", "secrets"):
                    return secrets
                return default

        monkeypatch.setattr(local, "get_settings", lambda: _S())
        # 密钥改由 platform_secrets 统一取(后台配置 > 环境变量);这里只测环境变量这一路
        import cogni_agent.config as cfg_mod
        from cogni_agent.governance_event_bus_service import platform_secrets as ps

        monkeypatch.setattr(cfg_mod, "get_settings", lambda: _S())
        monkeypatch.setattr(ps, "_cache", {})
        return local._inject_platform_secrets(headers)

    def test_placeholder_is_replaced_at_call_time(self, monkeypatch):
        out = self._inject({"xpftkey": "{{data_platform_key}}"}, {"data_platform_key": "REAL"}, monkeypatch)
        assert out == {"xpftkey": "REAL"}

    def test_missing_key_fails_loudly_not_with_a_401(self, monkeypatch):
        """把占位符原样发给对方,换回来的是一个 401 —— 看起来像接口坏了。"""
        with pytest.raises(ValueError, match="没有配置"):
            self._inject({"xpftkey": "{{data_platform_key}}"}, {"data_platform_key": ""}, monkeypatch)

    def test_other_headers_untouched(self, monkeypatch):
        out = self._inject({"Accept": "application/json"}, {"data_platform_key": "REAL"}, monkeypatch)
        assert out == {"Accept": "application/json"}

    def test_secret_is_not_in_the_general_variable_table(self):
        """放进通用变量表的话,一个同名的模型参数就能决定它的值,或被拼进 url 打到日志上。"""
        src = (ROOT / "cogni_agent" / "tool_center_service" / "sandbox" / "local.py").read_text(encoding="utf-8")
        body = src[src.index("async def _execute_http"):src.index("async def _execute_db")]
        assert "_inject_platform_secrets(headers)" in body
        assert "secrets" not in src[src.index("def system_vars"):src.index("def _render_template_str")]


class TestSupplierExtraction:
    def test_path_matches_the_http_tool_result_shape(self):
        """HTTP 工具的结果外面包着一层 {status_code, body};接口原始返回在 body 里。
        (本地连不到数据平台,这里按文档 1.12 的返回结构钉;脚本 --apply 后会在你的环境里真跑一次核对。)"""
        from cogni_agent.runtime_service.engine.tools_adapter import _extract_path

        result = {"status_code": 200, "body": {"status": True, "code": 0, "msg": "",
                                               "data": [{"scenicId": "PFTSCA01018088", "supplierId": [1757026]}]}}
        assert _extract_path(result, _script().SUPPLIER_VALUE_PATH) == [1757026]

    def test_scenic_without_supplier_mapping_is_a_clear_error(self):
        from cogni_agent.runtime_service.engine.tools_adapter import _extract_path

        with pytest.raises(KeyError):
            _extract_path({"status_code": 200, "body": {"data": []}}, "body.data[0].supplierId")


class _Spec:
    def __init__(self, props, required=()):
        self.name = "票务-今日客流统计"
        self.validate_schema = {"type": "object", "properties": props, "required": list(required)}
        self.json_schema = self.validate_schema


class TestCtxDefaults:
    SCHEMA = {"scenicId": {"type": "string", "x-ctx-default": "$ctx.scenicId"},
              "timeGran": {"type": "string"}}

    def test_model_given_value_is_respected(self):
        """用户在话里点名了景区,模型填了 —— 就用它(景区账号越权由后面的范围校验拦)。"""
        from cogni_agent.runtime_service.engine.tools_adapter import _apply_ctx_defaults

        out = _apply_ctx_defaults(_Spec(self.SCHEMA), {"scenicId": "B"}, scenic_id="A")
        assert out["scenicId"] == "B"

    def test_missing_value_is_filled_from_the_current_scenic(self):
        from cogni_agent.runtime_service.engine.tools_adapter import _apply_ctx_defaults

        out = _apply_ctx_defaults(_Spec(self.SCHEMA), {"timeGran": "month"}, scenic_id="PFTSCA01018088")
        assert out == {"timeGran": "month", "scenicId": "PFTSCA01018088"}

    def test_no_scenic_anywhere_asks_the_user(self):
        """集团账号、没点名、会话里也没有 —— 问,绝不编一个。"""
        from cogni_agent.runtime_service.engine.tools_adapter import _apply_ctx_defaults

        with pytest.raises(ClarificationNeeded) as ei:
            _apply_ctx_defaults(_Spec(self.SCHEMA), {}, scenic_id=None)
        assert ei.value.slot == "scenic_id"
        assert "哪个景区" in ei.value.question

    def test_empty_string_counts_as_not_given(self):
        from cogni_agent.runtime_service.engine.tools_adapter import _apply_ctx_defaults

        out = _apply_ctx_defaults(_Spec(self.SCHEMA), {"scenicId": ""}, scenic_id="A")
        assert out["scenicId"] == "A"

    def test_params_without_the_declaration_are_untouched(self):
        from cogni_agent.runtime_service.engine.tools_adapter import _apply_ctx_defaults

        out = _apply_ctx_defaults(_Spec({"scenicId": {"type": "string"}}), {}, scenic_id="A")
        assert out == {}, "没有声明就不补 —— 别的工具的 scenicId 语义可能完全不同"

    def test_filled_before_the_first_validation(self):
        """先校验的话会先撞上「缺少必填参数」,反过来引导模型去编一个景区 ID。"""
        src = (ROOT / "cogni_agent" / "runtime_service" / "engine" / "tools_adapter.py").read_text(encoding="utf-8")
        i_fill = src.index("args = _apply_ctx_defaults(spec, args, scenic_id=scenic_id)")
        # v3.59:校验时剔除引用系统参数的入参(它们不在模型可见 Schema 里),写法变了,顺序不变
        i_validate = src.index("jsonschema.validate({k: v for k, v in args.items() if k not in _ref_args}")
        i_params = src.index("_inj = _sp.apply_to_args(")
        assert i_fill < i_params < i_validate


class TestParamSourceNoLongerSilent:
    @run_async
    async def test_missing_ctx_scenic_raises_clarification(self):
        """原先是 `continue` —— supplierId 被悄悄跳过,接口拿回空数据,模型说「没有数据」。"""
        from cogni_agent.runtime_service.engine import tools_adapter as ta

        class Spec:
            name = "票务-今日客流统计"
            param_sources = {"supplierId": {"tool": "sup", "key": "$ctx.scenicId",
                                            "path": "body.data[0].supplierId"}}

        with pytest.raises(ClarificationNeeded):
            await ta._resolve_param_sources(None, task_id="t", spec=Spec(), merged_args={},
                                            bound_tools={}, scenic_id=None, role="super_admin",
                                            user_id="u", depth=0)


class TestVisibleSchema:
    def test_ctx_default_params_are_optional_for_the_model(self):
        """留着必填,模型每次都会被推着去填一个景区 ID —— 手上没有就编。"""
        from cogni_agent.runtime_service.engine.resolver import _relax_ctx_defaults

        schema = {"type": "object",
                  "properties": {"scenicId": {"type": "string", "description": "景区ID",
                                              "x-ctx-default": "$ctx.scenicId"},
                                 "timeGran": {"type": "string"}},
                  "required": ["scenicId", "timeGran"]}
        out = _relax_ctx_defaults(schema)
        assert out["required"] == ["timeGran"]
        assert "scenicId" in out["properties"], "看得见 —— 用户点名景区时它要能填"
        assert "不填则使用当前景区" in out["properties"]["scenicId"]["description"]
        assert schema["required"] == ["scenicId", "timeGran"], "不能改到原 schema"

    def test_untouched_when_nothing_declared(self):
        from cogni_agent.runtime_service.engine.resolver import _relax_ctx_defaults

        schema = {"type": "object", "properties": {"a": {}}, "required": ["a"]}
        assert _relax_ctx_defaults(schema) is schema


class TestClarificationFinalize:
    def test_executor_ends_the_turn_as_a_question_not_a_failure(self):
        """这一轮没有出错,它在等用户补一句话 —— 记成失败会进告警、前端显示红字。"""
        src = (ROOT / "cogni_agent" / "runtime_service" / "engine" / "executor.py").read_text(encoding="utf-8")
        i = src.index("except ClarificationNeeded as exc:")
        block = src[i:i + 2600]
        assert '"needs_clarification": True' in block, "与 Triager clarify 同一个结果形状,前端才不用改"
        assert '_finalize_task(task_id, "COMPLETED"' in block
        assert "scenic_choices" in block, "问景区时要给出可选项,不然集团账号记不住那串 ID"

    def test_clarification_is_caught_before_the_generic_handler(self):
        src = (ROOT / "cogni_agent" / "runtime_service" / "engine" / "executor.py").read_text(encoding="utf-8")
        assert src.index("except ClarificationNeeded") < src.index("except Exception as exc:  # noqa: BLE001 - 任何未预期异常")

    def test_raised_outside_the_tool_catch_all(self):
        """工具执行包在一个 except Exception 里(失败回给模型当错误);
        澄清要是在那里面抛,会被吞成一句「工具执行出错」。"""
        src = (ROOT / "cogni_agent" / "runtime_service" / "engine" / "tools_adapter.py").read_text(encoding="utf-8")
        body = src[src.index("async def execute_single_tool_call"):]
        i_fill = body.index("_apply_ctx_defaults(spec, args")
        i_try = body.index("if spec.type == \"builtin\":")
        assert i_fill < i_try


class TestPrerequisiteAutoInclude:
    def test_resolver_includes_declared_prerequisites(self):
        """每绑一个要 supplierId 的接口都得记得把供应商接口也绑上,忘了一次就永远调不通。"""
        src = (ROOT / "cogni_agent" / "runtime_service" / "engine" / "resolver.py").read_text(encoding="utf-8")
        assert "tools = await _include_param_source_tools(db, tools" in src
        body = src[src.index("async def _include_param_source_tools"):src.index("def _drop_superseded_builtins")]
        assert "_load_authorized_tools(" in body, "必须走同一道授权,不能因为是依赖就绕过权限"
        assert "param_source_tool_missing" in body, "带不进来要说清楚,而不是等调用时才失败"


# ══════════════════════════════════════════════════════════════════════
# 四、本轮景区:指定 > 当前归属 > 澄清
# ══════════════════════════════════════════════════════════════════════

def _tables():
    return [t for t in Base.metadata.sorted_tables
            if not any(type(c.type).__module__.startswith("pgvector") for c in t.columns)]


@pytest.fixture
def scenic_db():
    from cogni_agent.user_center_service.models import SysScenic

    async def setup():
        engine = common_db.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=_tables()))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_tables()))
        async with common_db.get_sessionmaker()() as db:
            db.add_all([
                SysScenic(scenic_id="PFTSCA01018088", scenic_name="九龙潭名胜风景区", is_enabled=True),
                SysScenic(scenic_id="PFTSCA01009835", scenic_name="泰宁大金湖风景区", is_enabled=True),
                SysScenic(scenic_id="PFTSCX00012325", scenic_name="泰宁寨下大峡谷", is_enabled=True),
                SysScenic(scenic_id="PFTSCA01000001", scenic_name="九龙潭", is_enabled=True),
            ])
            await db.commit()
    asyncio.run(setup())
    yield


class _User:
    def __init__(self, scenic_id=None):
        self.scenic_id = scenic_id


class _Conv:
    def __init__(self, scenic_id=None):
        self.id, self.scenic_id = "c1", scenic_id


async def _from_text(text):
    from cogni_agent.gateway_service import scenic_binding as sb

    async with common_db.get_sessionmaker()() as db:
        return await sb.scenic_from_text(db, text)


async def _resolve(user, conv, msg):
    from cogni_agent.gateway_service import scenic_binding as sb

    async with common_db.get_sessionmaker()() as db:
        return await sb.resolve_turn_scenic(db, user=user, conv=conv, message=msg)


@pytest.mark.usefixtures("scenic_db")
class TestScenicFromText:
    @run_async
    async def test_scenic_id_in_the_message(self):
        sid, _ = await _from_text("PFTSCA01009835 近7天客流")
        assert sid == "PFTSCA01009835"

    @run_async
    async def test_lowercase_id_still_counts(self):
        sid, _ = await _from_text("pftsca01009835 的票务")
        assert sid == "PFTSCA01009835"

    @run_async
    async def test_an_id_shape_that_is_not_a_scenic_is_ignored(self):
        """凭形状认会把订单号当景区 —— 判定依据是库里有没有。"""
        sid, _ = await _from_text("订单 ORDER20260921001 的状态")
        assert sid is None

    @run_async
    async def test_full_name_in_a_sentence(self):
        sid, why = await _from_text("泰宁大金湖风景区上周的客流怎么样")
        assert sid == "PFTSCA01009835" and "景区名" in why

    @run_async
    async def test_longest_name_wins(self):
        """「九龙潭名胜风景区」不能被一个叫「九龙潭」的短名抢走。"""
        sid, _ = await _from_text("九龙潭名胜风景区的票务")
        assert sid == "PFTSCA01018088"

    @run_async
    async def test_short_alias_as_a_clarification_answer(self):
        """回答澄清时最常见的就是一个简称。"""
        sid, _ = await _from_text("大金湖")
        assert sid == "PFTSCA01009835"

    @run_async
    async def test_ambiguous_alias_is_not_guessed(self):
        """「泰宁」对得上两个 —— 挑错了取回来的是另一个景区的数,而回答看起来完全正常。"""
        sid, why = await _from_text("泰宁")
        assert sid is None and "2 个景区" in why

    @run_async
    async def test_two_ids_is_ambiguous(self):
        sid, _ = await _from_text("对比 PFTSCA01018088 和 PFTSCA01009835")
        assert sid is None

    @run_async
    async def test_short_name_inside_a_sentence(self):
        """用户第一句话就这么说。点名了景区还被反问「查哪个景区」,只会让人觉得系统没在听。"""
        sid, _ = await _from_text("大金湖近7天客流")
        assert sid == "PFTSCA01009835"

    @run_async
    async def test_generic_word_does_not_match_every_scenic(self):
        """「风景区」三个字和每个景区都对得上 —— 算核心名时要先去掉通用后缀。"""
        sid, _ = await _from_text("这个风景区最近口碑怎么样")
        assert sid is None

    @run_async
    async def test_no_scenic_mentioned(self):
        sid, _ = await _from_text("近7天的客流趋势怎么样,和上周比有没有下降")
        assert sid is None


@pytest.mark.usefixtures("scenic_db")
class TestResolveTurnScenic:
    @run_async
    async def test_scenic_account_uses_current_attribution(self):
        sid, src = await _resolve(_User("PFTSCA01018088"), _Conv("PFTSCA01018088"), "客流")
        assert sid == "PFTSCA01018088" and "当前归属" in src

    @run_async
    async def test_rebound_scenic_account_follows_the_new_attribution(self):
        """改了归属之后,旧会话不能还拿着旧景区去取数 —— 那是别人家的数据了。"""
        conv = _Conv("PFTSCA01009835")
        sid, _ = await _resolve(_User("PFTSCA01018088"), conv, "客流")
        assert sid == "PFTSCA01018088"
        assert conv.scenic_id == "PFTSCA01018088", "会话上记的也要跟着改"

    @run_async
    async def test_scenic_account_cannot_switch_by_naming_another(self):
        sid, _ = await _resolve(_User("PFTSCA01018088"), None, "PFTSCA01009835 的客流")
        assert sid == "PFTSCA01018088"

    @run_async
    async def test_group_account_naming_a_scenic(self):
        conv = _Conv(None)
        sid, src = await _resolve(_User(None), conv, "大金湖近7天客流")
        assert sid == "PFTSCA01009835" and "消息中指定" in src
        assert conv.scenic_id == "PFTSCA01009835", "要记到会话上,不然下一句又被问一遍"

    @run_async
    async def test_group_account_follow_up_keeps_the_scenic(self):
        sid, src = await _resolve(_User(None), _Conv("PFTSCA01009835"), "那上个月呢")
        assert sid == "PFTSCA01009835" and "此前指定" in src

    @run_async
    async def test_group_account_can_switch_scenic_mid_conversation(self):
        conv = _Conv("PFTSCA01009835")
        sid, _ = await _resolve(_User(None), conv, "换成九龙潭名胜风景区看看")
        assert sid == "PFTSCA01018088"

    @run_async
    async def test_group_account_with_nothing_returns_none_for_clarification(self):
        sid, _ = await _resolve(_User(None), _Conv(None), "近7天客流")
        assert sid is None


class TestRoutesUseTheResolver:
    @pytest.mark.parametrize("rel", ["cogni_agent/gateway_service/routes_chat.py",
                                     "cogni_agent/gateway_service/routes_app.py"])
    def test_both_chat_entries_resolve_the_turn_scenic(self, rel):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "resolve_turn_scenic(" in src
        assert "conv_svc.set_scenic(" in src, "会话对象来自另一个 session,就地改不会落库"
