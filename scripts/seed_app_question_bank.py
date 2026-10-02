#!/usr/bin/env python
"""
APP 问题库/指标定义种子数据导入(19 号文档 S2 + 方案 A 指标名)。

    uv run python scripts/seed_app_question_bank.py

数据来源:01 号文档《APP 问题模板库数据整理》(PRD 96 条):
- 44 条问题列表 + 12 条选择器独有问题 = 56 行问题库
  (selector 43 条按「文本以 question_bank 版本统一」合并到 44 条上标 dimension_name,
   近似重复的 selector 节点由对应问题承载,见 19 号 D1)
- 9 条指标定义
- 方案 A(0052):dimension_name 非空行(=选择器第三级叶子)另配 metric_name「指标」展示名,
  一个指标=一个叶子问题=该行 question_id;APP 第三级显示指标名而非整句问题。

幂等:全删重灌(按 scenic_id IS NULL 的通用模板范围)。注意重灌会清掉 skill_id 绑定,
完整重建顺序 = 本脚本 → create_p0_group_skills.py(建技能 + 回填 skill_id)。
P0 命中 14 条绑定执行技能,skill_id 由 create_p0_group_skills.py 回填(S5)。
"""
from __future__ import annotations

import asyncio

from sqlalchemy import delete

from cogni_agent.common.db import get_engine
from cogni_agent.gateway_service.models import AppMetricDef, AppQuestionBank

# ── 指标定义(9 条,01 号文档 §4.1) ─────────────────────────────────────────
METRICS = [
    # theme, code, name, desc, warning, danger, time_node, strategy, priority
    ("passenger_flow", "inpark_ratio", "在园人数/最大承载数", "实时在园人数占最大承载量比例,安全红线指标", "≥75%", "≥90%", "小时", "realtime", "P0"),
    ("passenger_flow", "entry_speed", "当前入园速率", "近15分钟入园人数,防范入口瞬时聚集", "较近7日均值+50%", "较近7日均值+100%", "小时", "realtime", "P1"),
    ("ticket", "refund_rate", "退票率", "昨日退票量占售票量比例", ">5%", ">10%", "昨日", "daily_0am", "P1"),
    ("ticket", "revenue_achievement", "售票收入达成", "昨日售票收入同比昨日/去年同期", "同比下降>10%", "同比下降>30%", "昨日", "daily_0am", "P0"),
    ("public_opinion", "negative_ratio", "负面评价比例", "负面评价占总评价比例", ">10%", ">30%", "今日/昨天", "daily_0am", "P0"),
    ("public_opinion", "negative_surge", "负面舆情激增率", "负面声量环比昨日增幅", ">50%", ">100%", "今日/昨天", "realtime", "P0"),
    ("public_opinion", "breakout_event", "突发热点事件", "是否出现传播量异常激增的单点事件", "声量异常苗头", "形成扩散/热搜", "今日/昨天", "realtime", "P0"),
    ("ota_direct_sales", "ota_score", "OTA平台评分", "昨日主流OTA平台综合评分(与舆情联动)", "单日下降≥0.1分", "单日下降≥0.5分", "昨日", "daily_0am", "P1"),
    ("ota_direct_sales", "channel_anomaly", "渠道出票异常", "昨日单渠道出票量环比波动", "下降>30%", "下降>50%", "昨日", "daily_0am", "P0"),
]

# ── 问题库(56 条:44 条问题列表 + 12 条选择器独有) ──────────────────────────
# 字段:theme, question, metric_code, metric_name, scenario, time_period, priority, dimension, return_format
# metric_name=选择器第三级「指标」展示名(dimension 非空行必填,如 实时人数/承载率);NULL 不进选择器
# 周期枚举:today/yesterday/week_to_date/last_7_days/month_to_date/next_3_days/tencent_report
# return_format=问题级图表返回形态(22 号文档):
#   card  = 单值/快照类(实时人数/收入/退票量/承载率/进度),以数据卡片呈现;
#   line  = 时序趋势类(分时/近7天/月度走势),折线图;
#   bar   = 多对象数量对比/排名(渠道/票种/平台/时段/客流环比),条形图;
#   pie   = 构成占比类(来源/票种/渠道/情感正负),饼状图;
#   auto  = 判断/异常/文案类不固定形态,由技能提示词引导 LLM 自由决定(可无图)。
QUESTIONS = [
    # 客流 14
    ("passenger_flow", "今日实时在园人数是多少？", "inpark_ratio", "实时在园人数", "日常巡检/应急", "today", "P0", "在园人员", "card"),
    ("passenger_flow", "今日实时在园人数与昨日同期相比如何？", "inpark_ratio", None, "日常巡检/应急", "today", "P0", None, "card"),
    ("passenger_flow", "今日客流是否触达预警阈值？", "inpark_ratio", None, "应急响应", "today", "P0", None, "auto"),
    ("passenger_flow", "今日客流当前承载率多少？", "inpark_ratio", "当前承载率", "应急响应", "today", "P0", "在园人员", "card"),
    ("passenger_flow", "今日入园高峰是哪个时段？", None, "入园高峰时段", "日常巡检", "today", "P1", "入园趋势", "bar"),
    ("passenger_flow", "今日各时段人流情况？", None, "分时人流", "日常巡检", "today", "P1", "入园趋势", "line"),  # selector 独有
    ("passenger_flow", "本周客流高峰出现在哪几天？", None, None, "经营分析", "week_to_date", "P1", None, "bar"),
    ("passenger_flow", "本周客流高峰出现在哪个时段？", None, None, "经营分析", "week_to_date", "P1", None, "bar"),
    ("passenger_flow", "近7天客流趋势如何？", None, "客流趋势", "经营分析", "last_7_days", "P1", "入园趋势", "line"),
    ("passenger_flow", "近7天客流与上周环比变化？", None, "客流环比", "经营分析", "last_7_days", "P1", "入园趋势", "bar"),
    ("passenger_flow", "今日各景区区域游客密度分布情况？", None, "区域客流密度", "日常巡检/应急", "tencent_report", "P1", "区域分布", "bar"),
    ("passenger_flow", "今日各景区哪个区域最拥挤？", None, "拥挤区域", "日常巡检/应急", "tencent_report", "P1", "区域分布", "bar"),
    ("passenger_flow", "当前游客来源分布？", None, "游客来源分布", "经营分析", "tencent_report", "P2", "客流来源", "pie"),
    ("passenger_flow", "当前游客来源省份分布？", None, "来源省份分布", "经营分析", "tencent_report", "P2", "客流来源", "pie"),  # selector 独有
    ("passenger_flow", "当前游客来源城市分布？", None, "来源城市分布", "经营分析", "tencent_report", "P2", "客流来源", "pie"),  # selector 独有
    ("passenger_flow", "当前游客中男女比例是怎样的？", None, "游客性别结构", "经营分析", "tencent_report", "P2", "在园人员", "pie"),
    ("passenger_flow", "当前游客中年龄结构是怎样的？", None, "游客年龄结构", "经营分析", "tencent_report", "P2", "在园人员", "pie"),
    # 票务 11 + 6 selector 独有
    ("ticket", "昨日售票收入及与前日对比情况？", "revenue_achievement", "售票收入", "日常巡检", "yesterday", "P0", "售票概况", "card"),
    ("ticket", "昨日售票总量？", None, "售票总量", "日常巡检", "yesterday", "P0", "售票概况", "card"),  # selector 独有
    ("ticket", "昨日退票量有多少？", "refund_rate", "退票量", "日常巡检/应急", "yesterday", "P0", "退票预售", "card"),
    ("ticket", "昨日退票率是否异常？", "refund_rate", "退票率", "日常巡检/应急", "yesterday", "P0", "退票预售", "auto"),
    ("ticket", "本月累计收入及目标达成进度如何？", "revenue_achievement", "月度收入达成", "经营分析", "month_to_date", "P0", "售票概况", "card"),
    ("ticket", "本月目标达成进度如何？", "revenue_achievement", None, "经营分析", "month_to_date", "P1", None, "card"),
    ("ticket", "本月售票量及收入趋势？", None, "本月售票趋势", "经营分析", "month_to_date", "P1", "售票概况", "line"),  # selector 独有
    ("ticket", "昨日各票种销售量是多少？", None, "票种销量", "经营分析", "yesterday", "P0", "票种分析", "bar"),  # selector 独有
    ("ticket", "本周各票种销售占比是多少？", None, "票种占比", "经营分析", "week_to_date", "P1", "票种分析", "pie"),
    ("ticket", "本周哪种票最畅销？", None, "畅销票种", "经营分析", "week_to_date", "P1", "票种分析", "bar"),
    ("ticket", "本周线上渠道和线下窗口售票比例如何？", None, "渠道占比", "经营分析", "week_to_date", "P1", "渠道分析", "pie"),
    ("ticket", "本周哪个OTA直销渠道贡献最多售票量？", None, "渠道贡献", "经营分析", "week_to_date", "P1", "渠道分析", "bar"),
    ("ticket", "昨日自营渠道销售量及占比？", None, "自营销量占比", "经营分析", "yesterday", "P1", "渠道分析", "pie"),  # selector 独有
    ("ticket", "昨日退票原因分析？", None, "退票原因", "经营分析", "yesterday", "P1", "退票预售", "pie"),  # selector 独有
    ("ticket", "未来3天预售量如何？", None, "未来3天预售量", "经营分析", "next_3_days", "P2", "退票预售", "bar"),
    ("ticket", "未来3天预售量变化情况？", None, None, "经营分析", "next_3_days", "P2", None, "auto"),
    # 舆情 11 + 2 selector 独有
    ("public_opinion", "当前有没有突发热点事件需要关注？", "breakout_event", None, "应急响应", "today", "P0", None, "auto"),
    ("public_opinion", "当前舆情情感倾向如何？", "negative_ratio", "情感倾向", "日常巡检", "today", "P0", "情感分析", "pie"),
    ("public_opinion", "当前舆情情感正面/负面占比？", "negative_ratio", None, "日常巡检", "today", "P0", None, "pie"),
    ("public_opinion", "负面舆情较前日是否有激增？增幅多少？", "negative_surge", None, "应急响应", "today", "P0", None, "bar"),
    ("public_opinion", "哪个平台负面评价最多？", None, "负面高发平台", "日常巡检", "today", "P1", "平台监控", "bar"),
    ("public_opinion", "当前各平台舆情分布情况？", None, "平台舆情分布", "日常巡检", "today", "P1", "平台监控", "pie"),  # selector 独有
    ("public_opinion", "当前各OTA评分环比变化情况？", "ota_score", "OTA评分变化", "日常巡检", "today", "P1", "平台监控", "bar"),  # selector 独有
    ("public_opinion", "游客最常提及的痛点是什么？", None, "游客痛点", "经营分析", "today", "P1", "情感分析", "bar"),
    ("public_opinion", "近7天舆情趋势如何？", None, "舆情趋势", "经营分析", "last_7_days", "P2", "趋势对比", "line"),
    ("public_opinion", "近7天舆情较上周变化情况？", None, "舆情环比", "经营分析", "last_7_days", "P2", "趋势对比", "bar"),
    ("public_opinion", "本月舆情走势？", None, "本月舆情走势", "经营分析", "month_to_date", "P1", "趋势对比", "line"),  # selector 独有
    ("public_opinion", "当前正面舆情有哪些亮点可以用于宣传？", None, "正面舆情亮点", "经营分析", "today", "P2", "情感分析", "auto"),
    # OTA 直销 6 + 1 selector 独有
    ("ota_direct_sales", "当前各渠道核销率如何？", None, "核销率", "日常巡检", "yesterday", "P0", "订单核销", "bar"),
    ("ota_direct_sales", "本周各渠道订单情况？", None, "订单概况", "经营分析", "week_to_date", "P0", "订单核销", "bar"),
    ("ota_direct_sales", "本周各渠道订单与上周对比？", None, "订单环比", "经营分析", "week_to_date", "P1", "历史对比", "bar"),
    ("ota_direct_sales", "本周渠道出票是否异常？", "channel_anomaly", "出票异常", "日常巡检", "week_to_date", "P0", "历史对比", "auto"),  # selector 独有
    ("ota_direct_sales", "当前各渠道出票量分别是多少？", None, "渠道出票量", "经营分析", "yesterday", "P2", "订单核销", "bar"),
    ("ota_direct_sales", "当前各渠道客单价是多少？", None, "客单价", "经营分析", "yesterday", "P2", "订单核销", "bar"),
    ("ota_direct_sales", "当前自营渠道与OTA渠道占比如何？", None, None, "经营分析", "yesterday", "P2", None, "pie"),
    # 卡片主题报告(20 号 §六:5 卡片 = 定时预热的完整 LLM 结果;P0 全量;返卡片=预热摘要卡片形态)
    ("passenger_flow", "【主题解读】客流", None, None, "日常巡检", "today", "P0", None, "card"),
    ("ticket", "【主题解读】票务", None, None, "日常巡检", "yesterday", "P0", None, "card"),
    ("public_opinion", "【主题解读】舆情", None, None, "日常巡检", "today", "P0", None, "card"),
    ("ota_direct_sales", "【主题解读】OTA直销", None, None, "日常巡检", "yesterday", "P0", None, "card"),
]

# P0 预热策略(20 号文档定稿):票务昨日 3 条 + OTA 核销率 + 4 条卡片主题报告 → 每日 02:00
PREWARM_DAILY = {
    "昨日售票收入及与前日对比情况？",
    "昨日退票量有多少？",
    "昨日退票率是否异常？",
    "当前各渠道核销率如何？",
    "【主题解读】客流",
    "【主题解读】票务",
    "【主题解读】舆情",
    "【主题解读】OTA直销",
}

# tencent_report 类数据源未接入(01 号文档待确认项):active=false
INACTIVE_TENCENT = True

# 24 号(预热策略重审)过渡决策:P1/P2 先不展示(active=false)——未预热点击=实时执行
# 等 10-30s,体验不可控且 today 类时效矛盾未定档;技能绑定/param_template/回填全保留,
# 预热策略落地后翻 ACTIVE_PRIORITY_ONLY 放开(或逐档放开)。P0/主题解读不受影响。
ACTIVE_PRIORITY_ONLY = "P0"

# 参数模板(19 号 S7.5):time_period → 基准日期参数(代码展开,零 LLM)。
# 相对参数(对比期)不配——L3 x-default-rule 规则引擎按基准自动平移补全。
PARAM_TEMPLATES = {
    "today": {"travelDate": "{{today}}"},
    "yesterday": {"startTime": "{{yesterday_start}}", "endTime": "{{yesterday_end}}"},
    "week_to_date": {"startTime": "{{week_start}} 00:00:00", "endTime": "{{now}}"},
    "month_to_date": {"startTime": "{{month_start}} 00:00:00", "endTime": "{{now}}"},
    "last_7_days": {"startTime": "{{days_7_ago}} 00:00:00", "endTime": "{{today_end}}"},
    "next_3_days": {"startTime": "{{tomorrow}} 00:00:00", "endTime": "{{plus_3d_end}} 23:59:59"},
}


async def main() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = get_engine()
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db, db.begin():
        # 幂等:清空通用模板范围(景区专属行不动)
        await db.execute(delete(AppQuestionBank).where(AppQuestionBank.scenic_id.is_(None)))
        await db.execute(delete(AppMetricDef))

        db.add_all([
            AppMetricDef(theme_code=t, metric_code=c, metric_name=n, metric_desc=d,
                         warning_rule=w, danger_rule=x, time_node=tn, update_strategy=s, priority=p)
            for (t, c, n, d, w, x, tn, s, p) in METRICS
        ])

        rows = []
        for i, (theme, q, metric, metric_name, scenario, period, priority, dimension,
                rfmt) in enumerate(QUESTIONS):
            is_p0 = priority == "P0"
            rows.append(AppQuestionBank(
                scenic_id=None,
                theme_code=theme,
                question_text=q,
                metric_code=metric,
                metric_name=metric_name,
                scenario=scenario,
                time_period=period,
                priority=priority,
                dimension_name=dimension,
                return_format=rfmt,
                skill_id=None,  # S5 技能创建后回填(23 号:直接绑技能,不再经组合)
                param_template=PARAM_TEMPLATES.get(period) if is_p0 else None,
                prewarm_enabled=q in PREWARM_DAILY,
                prewarm_point=None,  # 调度按业务日,不再有「每日 N 点」概念
                sort_order=i,
                active=(
                    not (INACTIVE_TENCENT and period == "tencent_report")
                    and (ACTIVE_PRIORITY_ONLY is None or priority == ACTIVE_PRIORITY_ONLY)
                ),
            ))
        db.add_all(rows)

    p0 = sum(1 for r in QUESTIONS if r[6] == "P0")
    dim = sum(1 for r in QUESTIONS if r[7])
    leaf = sum(1 for r in QUESTIONS if r[7] and not r[3])
    inactive = sum(1 for r in QUESTIONS if r[5] == "tencent_report" and INACTIVE_TENCENT)
    hidden = sum(1 for r in QUESTIONS
                 if ACTIVE_PRIORITY_ONLY is not None and r[6] != ACTIVE_PRIORITY_ONLY
                 and not (INACTIVE_TENCENT and r[5] == "tencent_report"))
    hidden_part = f" / P1P2 暂不展示 {hidden}" if hidden else ""
    fixed = sorted({r[8] for r in QUESTIONS if r[8] != "auto"})
    print(f"导入完成: 指标 {len(METRICS)} 条 | 问题 {len(QUESTIONS)} 条"
          f"(P0 {p0} / 带选择器维度 {dim} / 选择器叶子缺指标名 {leaf} / tencent_report 下架 {inactive}"
          f"{hidden_part})")
    print(f"return_format 标注: auto {sum(1 for r in QUESTIONS if r[8]=='auto')} 条自由输出,"
          f"固定形态 {sum(1 for r in QUESTIONS if r[8]!='auto')} 条: {fixed}")


if __name__ == "__main__":
    asyncio.run(main())
