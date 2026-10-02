# -*- coding: utf-8 -*-
"""内置数据工具与数据留存。

这一组要守的不是「算得对」——pandas 算得对是它自己的事 —— 而是**算错时会不会被看出来**。
模型拿到一个坏结果不会停下,它会照着坏结果写一段通顺的结论。所以每条用例问的都是
同一件事:出岔子的时候,回给模型的那句话能不能让它改正,而不是让它编下去。

四类必须钉住的「不报错的错」:

- **摘要之后明细丢了**:大结果压成摘要后原始行就没了,模型想分组求和却够不着明细,
  于是照着摘要里的合计估一个数 —— 这是留存缓冲要解决的根本问题;
- **句柄取不到被当成没数据**:一句「没有数据」会让模型直接回「本期无数据」,
  而真实情况是数据好好的,只是句柄过期或写错了;
- **两期对比错位**:两份数据行序不同、类别有缺,按位置相减会把携程的本期减去美团的上期,
  算出来的增长率照样是个像模像样的百分数;
- **关键词搜不到被当成业务没发生**:搜「飞猪」搜不到,不等于没有飞猪的订单,
  也可能是那一列里写的是「Fliggy」。
"""
from __future__ import annotations

import json

import pytest

from cogni_agent.runtime_service.engine import databuf
from cogni_agent.tool_center_service import builtin_tools as bi

TASK = "task-test-1"


@pytest.fixture(autouse=True)
def _clean():
    databuf.clear(TASK)
    yield
    databuf.clear(TASK)


ROWS = [
    {"渠道": "携程", "月份": "2026-08", "销售额": 1203560, "订单": 2456},
    {"渠道": "美团", "月份": "2026-08", "销售额": 986400, "订单": 1980},
    {"渠道": "携程", "月份": "2026-09", "销售额": 1450000, "订单": 2900},
    {"渠道": "美团", "月份": "2026-09", "销售额": 870000, "订单": 1700},
    {"渠道": "抖音", "月份": "2026-09", "销售额": 320000, "订单": 640},
]


# ══════════════════════════════════════════════════════════════════════
# 一、数据留存:让被压缩掉的明细仍然可算
# ══════════════════════════════════════════════════════════════════════

def test_big_result_is_compressed_but_still_queryable():
    """**这条是整个设计的理由**。

    压缩本身是对的(不压就超上下文),但以前压完把原始行丢了,于是
    「按渠道分组求和」这种最基本的要求,模型反而做不到了 —— 它手里只有合计和几行样本。
    做不到的时候它通常不会说做不到,而是照着摘要里的数字估一个。
    """
    from cogni_agent.runtime_service.engine.toolresult import compress_or_truncate

    big = {"code": 0, "data": {"list": [
        {"渠道": ["携程", "美团", "抖音"][i % 3], "销售额": 1000 + i * 13} for i in range(1200)]}}
    raw_len = len(json.dumps(big, ensure_ascii=False))

    text = compress_or_truncate(big, task_id=TASK, tool_name="订单明细查询")
    assert len(text) < raw_len / 10, "没压下来的话上下文会被这一个返回吃光"
    assert "句柄 ds_1" in text, "压缩后必须告诉模型明细去哪儿取,否则它只能靠估"

    out = bi.builtin_table_query(dataset="ds_1", group_by="渠道", agg="销售额:sum", _task_id=TASK)
    assert out["row_count"] == 3
    # 1200 行一行都没进上下文,合计却是按全量算的
    total = sum(r["销售额"] for r in out["rows"])
    assert total == sum(1000 + i * 13 for i in range(1200))


def test_no_task_id_means_no_stash():
    """没有任务号就不留存 —— 没有归属就没有清理时机,留下来就是泄漏。"""
    from cogni_agent.runtime_service.engine.toolresult import compress_or_truncate

    big = {"list": [{"a": i} for i in range(300)]}
    text = compress_or_truncate(big, task_id=None, tool_name="x")
    assert "句柄" not in text


def test_datasets_are_isolated_between_tasks():
    """A 任务的数据不能被 B 任务读到 —— 两个任务往往属于不同景区、不同的人。"""
    databuf.stash("task-A", "工具", ROWS)
    assert databuf.fetch("task-B", "ds_1") is None
    databuf.clear("task-A")


def test_clear_removes_everything():
    """任务结束要清干净:这是业务明细,不该在内存里留过夜。"""
    databuf.stash(TASK, "工具", ROWS)
    assert databuf.catalog(TASK)
    databuf.clear(TASK)
    assert databuf.catalog(TASK) == []
    assert databuf.fetch(TASK, "ds_1") is None


def test_handles_are_never_reused():
    """句柄号只增不减。复用的话,模型拿着上一份数据的号去查新数据 ——
    而两份数据的列名往往一模一样,查出来的结果看不出任何异常。"""
    a = databuf.stash(TASK, "工具", ROWS)
    b = databuf.stash(TASK, "工具", ROWS)
    assert a["handle"] != b["handle"]


def test_old_datasets_are_evicted_but_newest_kept():
    """留太多会把内存吃光;丢就丢最早的 —— 模型用的通常是最近取到的那份。"""
    for _ in range(databuf.MAX_DATASETS_PER_TASK + 3):
        databuf.stash(TASK, "工具", ROWS)
    cat = databuf.catalog(TASK)
    assert len(cat) == databuf.MAX_DATASETS_PER_TASK
    assert cat[-1]["handle"] == f"ds_{databuf.MAX_DATASETS_PER_TASK + 3}"


def test_missing_dataset_says_so_loudly():
    """**取不到 ≠ 没数据**。

    只回一句「没有数据」的话,模型会直接答「本期无数据」——
    而真实情况是数据好好的,只是句柄写错或过期了。所以必须说清是哪种,
    并把现有句柄列出来让它改。
    """
    databuf.stash(TASK, "工具", ROWS)
    out = bi.builtin_table_query(dataset="ds_99", _task_id=TASK)
    assert "不存在" in out["error"]
    assert "ds_1" in out["error"], "要告诉它现在有哪些句柄可用"
    assert "不要据此回答" in out["error"]


def test_no_dataset_and_no_rows_tells_the_model_what_to_do():
    out = bi.builtin_table_stats(_task_id=TASK)
    assert "先调用取数工具" in out["error"]


# ══════════════════════════════════════════════════════════════════════
# 二、统计画像
# ══════════════════════════════════════════════════════════════════════

def test_stats_reports_distribution_not_just_samples():
    out = bi.builtin_table_stats(rows=ROWS, columns="销售额", _task_id=TASK)
    col = out["columns"][0]
    assert col["kind"] == "number"
    assert col["sum"] == sum(r["销售额"] for r in ROWS)
    assert col["median"] and col["p25"] and col["p75"]


def test_stats_rejects_unknown_column_with_the_real_column_list():
    """列名写错是最常见的错。只说「失败」的话模型会换个同样错的写法再试一遍。"""
    out = bi.builtin_table_stats(rows=ROWS, columns="营业额", _task_id=TASK)
    assert "不存在" in out["error"] and "销售额" in out["error"]


# ══════════════════════════════════════════════════════════════════════
# 三、两期对比:按 key 对齐,不按位置
# ══════════════════════════════════════════════════════════════════════

def test_compare_aligns_by_key_not_by_position():
    """**两份数据的行序是错开的**。按位置相减会把携程的本期减去美团的上期,
    而算出来的增长率照样是个像模像样的百分数,没人看得出错。"""
    cur = [{"渠道": "携程", "销售额": 1450000}, {"渠道": "美团", "销售额": 870000}]
    prev = [{"渠道": "美团", "销售额": 986400}, {"渠道": "携程", "销售额": 1203560}]
    out = bi.builtin_table_compare(rows=cur, rows_b=prev, key="渠道", value="销售额")
    by = {i["渠道"]: i for i in out["items"]}
    assert by["携程"]["previous"] == 1203560, "携程必须对上携程"
    assert by["美团"]["previous"] == 986400
    assert by["携程"]["change_rate"].startswith("+")
    assert by["美团"]["change_rate"].startswith("-")


def test_compare_calls_out_appeared_and_disappeared_items():
    """只在一期出现的类别要单独标出来 —— 新增和消失恰恰是最该说的事,
    而按位置相减时它们会把后面所有行都顶错位。"""
    cur = [{"渠道": "抖音", "销售额": 320000}]
    prev = [{"渠道": "飞猪", "销售额": 50000}]
    out = bi.builtin_table_compare(rows=cur, rows_b=prev, key="渠道", value="销售额")
    st = {i["渠道"]: i["status"] for i in out["items"]}
    assert st["抖音"] == "新增" and st["飞猪"] == "已消失"


def test_compare_does_not_divide_by_zero_silently():
    """上期为 0 时不能编一个增长率出来,要说清算不了。"""
    out = bi.builtin_table_compare(rows=[{"k": "a", "v": 100}], rows_b=[{"k": "a", "v": 0}],
                                   key="k", value="v")
    assert "无法计算" in out["items"][0]["change_rate"]


def test_compare_needs_both_key_and_value():
    assert "必须指定" in bi.builtin_table_compare(rows=ROWS, rows_b=ROWS)["error"]


# ══════════════════════════════════════════════════════════════════════
# 四、透视 / 异常 / 趋势 / 搜索
# ══════════════════════════════════════════════════════════════════════

def test_pivot_builds_the_cross_table():
    out = bi.builtin_table_pivot(rows=ROWS, index="渠道", columns="月份", value="销售额",
                                 _task_id=TASK)
    by = {r["渠道"]: r for r in out["rows"]}
    assert by["携程"]["2026-08"] == 1203560 and by["携程"]["2026-09"] == 1450000
    assert by["抖音"]["2026-08"] == 0, "某月没有数据要补 0,不能缺列"


def test_outliers_picks_the_deviant_not_the_largest():
    """**最大 ≠ 异常**。旺季最大是正常的,淡季突然冒高才是异常。
    模型靠眼睛挑异常时挑的往往是最大的那几行。"""
    rows = [{"日期": f"2026-09-{d:02d}", "客流": 1000 + d} for d in range(1, 15)]
    rows.append({"日期": "2026-09-15", "客流": 9000})
    out = bi.builtin_table_outliers(rows=rows, column="客流", label="日期", _task_id=TASK)
    assert out["row_count"] == 1
    assert out["rows"][0]["日期"] == "2026-09-15"


def test_outliers_says_so_when_there_are_none():
    """没有异常也是一个结论,不能回一个空表让模型以为工具坏了。"""
    rows = [{"v": 100 + i % 3} for i in range(20)]
    out = bi.builtin_table_outliers(rows=rows, column="v", _task_id=TASK)
    assert out["row_count"] == 0 and "这本身也是一个结论" in out["note"]


def test_outliers_refuses_when_there_is_too_little_data():
    out = bi.builtin_table_outliers(rows=[{"v": 1}, {"v": 2}], column="v", _task_id=TASK)
    assert "太少" in out["error"]


def test_trend_gives_both_period_change_and_overall_direction():
    """逐期环比和整体方向是两个口径,要一起给 —— 单期涨了不等于趋势在涨。"""
    rows = [{"日期": f"2026-09-{d:02d}", "客流": 1000 + d * 50} for d in range(1, 16)]
    out = bi.builtin_table_trend(rows=rows, date_column="日期", value="客流", _task_id=TASK)
    assert "整体上升" in out["trend"]
    assert out["rows"][0]["环比"] == "", "第一期没有上一期可比,要留空而不是编一个 0%"
    assert out["rows"][1]["环比"].startswith("+")


def test_trend_rejects_unparseable_dates_clearly():
    out = bi.builtin_table_trend(rows=[{"d": "去年", "v": 1}], date_column="d", value="v",
                                 _task_id=TASK)
    assert "日期" in out["error"]


def test_search_finds_rows_without_knowing_the_column():
    out = bi.builtin_table_search(rows=ROWS, keyword="携程", _task_id=TASK)
    assert out["row_count"] == 2


def test_search_miss_offers_the_real_values_instead_of_an_empty_table():
    """**搜不到 ≠ 业务上没发生**,也可能是叫法不同(飞猪 / Fliggy)。
    回一个空表的话,模型会直接答「本期没有飞猪的订单」。"""
    out = bi.builtin_table_search(rows=ROWS, keyword="飞猪", columns="渠道", _task_id=TASK)
    assert out["row_count"] == 0
    assert "不代表业务上没有发生" in out["note"]
    assert "携程" in out["candidates"]["渠道"], "要给出这一列的实际取值,好换个词再找"


# ══════════════════════════════════════════════════════════════════════
# 五、算术与 JSON
# ══════════════════════════════════════════════════════════════════════

def test_calc_does_the_arithmetic_the_model_would_get_wrong():
    assert bi.builtin_calc(expression="(1450000-1203560)/1203560*100")["result"] == 20.48
    assert bi.builtin_calc(expression="round(1203560/2456, 2)")["result"] == 490.05


def test_calc_refuses_anything_that_is_not_arithmetic():
    """它只做数学。能执行代码的「计算器」就不是计算器了。"""
    for evil in ("__import__('os').system('ls')", "open('/etc/passwd').read()",
                 "().__class__.__bases__[0]", "lambda: 1"):
        assert "error" in bi.builtin_calc(expression=evil), evil


def test_calc_refuses_expressions_that_would_hang_the_task():
    """2**99999999 的底数只有 2,只卡底数是拦不住的 ——
    而它卡死的不是「算错一个数」,是整个任务没了响应。"""
    assert "error" in bi.builtin_calc(expression="2**99999999")
    assert "error" in bi.builtin_calc(expression="9**100000")
    assert bi.builtin_calc(expression="10**6")["result"] == 1000000


def test_calc_reports_division_by_zero():
    assert "除数为 0" in bi.builtin_calc(expression="100/0")["error"]


def test_json_peek_shows_structure_and_walks_a_path():
    data = {"code": 0, "data": {"list": [{"a": 1}] * 3, "total": 3}}
    out = bi.builtin_json_peek(data=data, path="data")
    assert out["type"] == "dict" and "list" in out["shape"]


def test_json_peek_bad_path_lists_what_is_available():
    """猜错层级取到空,会被当成「没有数据」。所以要告诉它这一层实际有什么。"""
    out = bi.builtin_json_peek(data={"code": 0, "data": {"list": []}}, path="data.items")
    assert "找不到" in out["error"] and "list" in out["available"]


# ══════════════════════════════════════════════════════════════════════
# 六、注册表本身
# ══════════════════════════════════════════════════════════════════════

def test_every_builtin_is_registered_consistently():
    """schema 必须能序列化 —— 塞不进 function schema 的工具,模型根本看不见它,
    而这件事不会报错,只表现为「这个工具从来没被调用过」。"""
    for name, spec in bi.BUILTIN.items():
        assert callable(spec["fn"]), name
        assert spec["description"].strip(), name
        json.dumps(spec["schema"])
        assert name.startswith("sys_"), f"{name} 缺少 sys_ 前缀(排障导出按它区分内置与业务工具)"


def test_data_tools_all_accept_a_dataset_handle():
    """表类工具必须都能按句柄取数。漏掉一个,那个工具就只能处理小数据 ——
    而它不会报错,只是在大数据场景里悄悄用不上。"""
    for name in ("sys_table_query", "sys_table_stats", "sys_table_pivot",
                 "sys_table_outliers", "sys_table_trend", "sys_table_search"):
        props = (bi.BUILTIN[name]["schema"].get("properties") or {})
        assert "dataset" in props, f"{name} 不支持数据句柄"


def test_builtin_names_are_covered_by_the_ui_grouping():
    """页面分组表漏了谁,谁就会掉进「其他」—— 不报错,只是运营找不到它。"""
    from cogni_agent.tool_center_service.routes import _BUILTIN_GROUPS

    missing = [n for n in bi.BUILTIN if n not in _BUILTIN_GROUPS]
    assert not missing, f"这些内置工具没有页面分组:{missing}"


# ══════════════════════════════════════════════════════════════════════
# 七、人工审批的 interrupt 必须是模块属性
# ══════════════════════════════════════════════════════════════════════

def test_interrupt_stays_a_module_attribute():
    """高风险工具的人工确认走 langgraph 的 interrupt,它**必须挂在模块上**。

    改成函数内 import 的话,模块照样能用、线上照样能审批 —— 但
    `patch("...tools_adapter.interrupt")` 会 AttributeError,于是审批那几条用例
    根本跑不起来。而「跑不起来」和「跑挂了」在一堆红色里长得一模一样,
    结果就是最该有人盯的那条链路悄悄没了测试覆盖,谁也不会发现。

    (这就是真实发生过的事:为了让没装 langgraph 的环境也能导入本模块,
    曾把它改成函数内 import,审批用例从此再没真正跑过。)
    """
    from cogni_agent.runtime_service.engine import tools_adapter

    assert hasattr(tools_adapter, "interrupt"), \
        "interrupt 不在模块上 —— 审批链路的用例会以 AttributeError 跳过,而不是真的验证"
    assert callable(tools_adapter.interrupt)


def test_builtin_required_params_never_count_as_missing_user_input():
    """内置工具的必填项不能进缺参判定 —— 否则会拿用户看不懂的参数名去反问他。

    内置工具是自动挂给每个技能的,它们的必填项是模型**调用那一刻**自己填的实参:
    sys_table_compare 的 key/value 来自它刚取到的那份数据,sys_calc 的 expression
    是它要算的式子。没有一个是「用户没给的信息」。

    算进来的后果不是报错,是**误反问**:一条本来能执行的提问被判出一堆假缺参
    (现场实测一次多出 8 个:key/value/index/column/date_column/keyword/expression/data),
    路由层据此跟用户要「expression」,用户完全不知道那是什么。
    """
    from cogni_agent.runtime_service.engine import intent
    from cogni_agent.runtime_service.engine.resolver import ToolSpec

    def spec(name, type_, required):
        return ToolSpec(id=name, name=name, description="", type=type_, risk_level="low",
                        json_schema={"type": "object", "required": required}, type_config={})

    class _Skill:
        name = "we"
        tools = [spec("sys_table_compare", "builtin", ["key", "value"]),
                 spec("sys_calc", "builtin", ["expression"]),
                 spec("雷达图指标", "http", ["scenicId", "startTime", "endTime"])]

    gaps = intent.required_param_gaps(skill_chain=[_Skill()], target_skills=["we"],
                                      filled={}, missing=[])
    assert gaps == ["scenicId", "startTime", "endTime"], gaps
    for noise in ("key", "value", "expression"):
        assert noise not in gaps, f"内置工具的 {noise} 不该被当成用户缺的参数"


# ══════════════════════════════════════════════════════════════════════
# 八、画图:用户要图,就真的给图
# ══════════════════════════════════════════════════════════════════════

_RADAR = [{"指标": "平均订单金额", "本景区": 141.59, "同类均值": 110.50},
          {"指标": "总游客数", "本景区": 90, "同类均值": 847},
          {"指标": "票房金额", "本景区": 21085, "同类均值": 29935.8},
          {"指标": "核销票量", "本景区": 463, "同类均值": 1099},
          {"指标": "口碑评分", "本景区": 3.79, "同类均值": 5.0}]


def test_chart_is_rendered_instead_of_ascii_art():
    """用户连着三轮要「图形化」,拿到的是字符拼的 ASCII 雷达图。

    系统**本来就能画** —— structured_output + chart_templates 支持
    line/bar/pie/radar,前端用 recharts 渲染。缺的只是一个让模型能调的入口:
    能力清单里没有画图工具,模型就以为自己没这个能力,只好用字符拼一个。
    """
    from cogni_agent.runtime_service.engine import databuf

    out = bi.builtin_chart(rows=_RADAR, chart_type="radar", title="景区 vs 同类",
                           label_column="指标", value_columns="本景区,同类均值",
                           _task_id=TASK)
    assert out.get("ok") is True
    charts = databuf.charts(TASK)
    assert len(charts) == 1 and charts[0]["type"] == "radar"
    assert "ASCII" in out["note"], "要明确告诉模型别再拼字符图了"


def test_radar_axes_get_their_own_scale():
    """**雷达图每根轴要有自己的量程。**

    共用一个量程的话这张图就废了:「票房 21085 元」和「口碑 3.79 分」画在一起,
    后者会贴在圆心上 —— 看图的人会以为口碑是 0。而图画出来了、颜色也对,
    没有任何地方会提示这张图是误导的。
    """
    from cogni_agent.runtime_service.engine import databuf

    bi.builtin_chart(rows=_RADAR, chart_type="radar", label_column="指标",
                     value_columns="本景区,同类均值", _task_id=TASK)
    inds = databuf.charts(TASK)[0]["option"]["radar"]["indicator"]
    by = {i["name"]: i["max"] for i in inds}
    assert by["口碑评分"] < 10, "口碑那根轴不能沿用票房的量程"
    assert by["票房金额"] > 20000
    # 留余量:贴边的点看不出是「刚好到顶」还是「超出了」
    assert by["平均订单金额"] > 141.59


def test_chart_numbers_come_from_the_data_not_the_model():
    """数字必须来自数据本身 —— 模型只说画哪几列。

    让模型把几十个数字重抄一遍是行不通的:它会把 21085 写成 21085.0、
    把 3.6234 四舍五入成 3.6,图的形状对、数却和取回来的那份对不上,没人会核对。
    """
    from cogni_agent.runtime_service.engine import databuf

    bi.builtin_chart(rows=_RADAR, chart_type="radar", label_column="指标",
                     value_columns="本景区", _task_id=TASK)
    vals = databuf.charts(TASK)[0]["option"]["series"][0]["data"][0]["value"]
    assert vals == [141.59, 90.0, 21085.0, 463.0, 3.79], vals


def test_chart_accepts_chinese_type_names():
    """模型多半会直接写「雷达图」「曲线」,不该因为这个就调不动。"""
    for name in ("雷达图", "曲线", "饼图", "柱状", "分布"):
        out = bi.builtin_chart(rows=_RADAR, chart_type=name, label_column="指标",
                               value_columns="本景区", _task_id=TASK)
        assert out.get("ok") is True, f"{name}: {out}"


def test_chart_works_from_a_dataset_handle():
    """大结果的明细在服务端,模型手里只有句柄 —— 画图也得能按句柄取数。"""
    from cogni_agent.runtime_service.engine import databuf

    h = databuf.stash(TASK, "取数工具", _RADAR)["handle"]
    out = bi.builtin_chart(dataset=h, chart_type="bar", label_column="指标",
                           value_columns="本景区", _task_id=TASK)
    assert out.get("ok") is True and out["points"] == 5


def test_chart_refuses_too_many_points_with_a_next_step():
    """几百个点画出来挤成一团,读图的人什么也看不出来。

    默默截断更糟 —— 图看着正常,其实只画了前一小段。要说清该怎么办。
    """
    out = bi.builtin_chart(rows=[{"d": i, "v": i} for i in range(200)],
                           chart_type="line", label_column="d", value_columns="v",
                           _task_id=TASK)
    assert "error" in out
    assert "分组聚合" in out["error"] or "TOP N" in out["error"], "要给下一步,不能只说不行"


def test_chart_rejects_text_columns_and_unknown_columns():
    for kwargs, expect in (
        (dict(label_column="维度", value_columns="本景区"), "不存在"),
        (dict(label_column="指标", value_columns="指标"), "没有可用的数值"),
        (dict(label_column="指标", value_columns=""), "必须指定"),
    ):
        out = bi.builtin_chart(rows=_RADAR, chart_type="bar", _task_id=TASK, **kwargs)
        assert expect in out.get("error", ""), out


def test_charts_are_cleared_with_the_task():
    """图和数据一样随任务结束清掉,不能串到下一个任务。"""
    from cogni_agent.runtime_service.engine import databuf

    bi.builtin_chart(rows=_RADAR, chart_type="bar", label_column="指标",
                     value_columns="本景区", _task_id=TASK)
    assert databuf.charts(TASK)
    databuf.clear(TASK)
    assert databuf.charts(TASK) == []


def test_executor_broadcasts_tool_charts():
    """钉住收尾时真的把 sys_chart 的图并进广播 —— 不并就等于白画。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "cogni_agent" / "runtime_service"
           / "engine" / "executor.py").read_text(encoding="utf-8")
    assert "_charts_from_tools(task_id)" in src, "sys_chart 画的图没有被广播出去"
    assert "parsed_output.charts = _tool_charts" in src


def test_chart_carries_both_render_forms():
    """一张图要同时带 ECharts option 和中性行列表。

    两个前端用的不是同一个图表库:APP(uni)装 echarts、吃 option;
    管理端操练台装 recharts、吃不了 option。只发一种的结果就是
    「图在一边有、另一边没有」—— 而回答正文里还写着「雷达图已生成 ✅」,
    看的人只会觉得系统坏了。

    两种形态由服务端**同一次计算**产出,所以不会出现两边数字对不上。
    """
    from cogni_agent.runtime_service.engine import databuf

    bi.builtin_chart(rows=_RADAR, chart_type="radar", title="五项核心指标",
                     label_column="指标", value_columns="本景区,同类均值", _task_id=TASK)
    c = databuf.charts(TASK)[0]
    assert c["option"]["radar"]["indicator"], "给 APP 的 ECharts option"
    t = c["table"]
    assert t["chart_type"] == "radar" and t["row_count"] == len(_RADAR)
    assert [f["name"] for f in t["fields"]] == ["指标", "本景区", "同类均值"]
    assert t["fields"][0]["role"] == "dimension"
    assert all(f["role"] == "metric" for f in t["fields"][1:])
    # 同源:两种形态里的数必须一致,否则「APP 上一个数、后台另一个数」
    from_option = c["option"]["series"][0]["data"][0]["value"]
    from_table = [r["本景区"] for r in t["rows"]]
    assert from_option == from_table, (from_option, from_table)
