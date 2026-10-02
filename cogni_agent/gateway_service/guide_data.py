"""
智能体主页静态引导数据（v1.1 阶段二）。

内容：5 张引导卡片 / 试试问我问题列表 / 三级平铺选择器。
这里基于 18 条预设问题给出合理默认映射；运营在问题库( AppQuestionBank )配了行即覆盖。

主题 code 与权限策略（gateway_service/policy.py 的 app 域 5 tab）保持一致：
overview / passenger_flow / ticket / public_opinion / ota_direct_sales
"""
from __future__ import annotations


# ────────────────────────────────────────────────────────────────
# 引导卡片（5 张主题卡片）
# question 带【主题解读】前缀：点击卡片 AI 智能解读区域 → 前端把
# 该字段原样作为 message 发送给 /api/app/chat，走普通对话链路。
# ────────────────────────────────────────────────────────────────

GUIDE_CARDS: list[dict] = [
    {
        "id": "overview",
        "title": "概览",
        "subtitle": "全局运营态势一屏掌握",
        "icon": "chart",
        "theme_code": "overview",
        "question": "【主题解读】概览",
    },
    {
        "id": "passenger_flow",
        "title": "客群",
        "subtitle": "客流趋势与客群结构洞察",
        "icon": "users",
        "theme_code": "passenger_flow",
        "question": "【主题解读】客群",
    },
    {
        "id": "ticket",
        "title": "票务",
        "subtitle": "票种销售与退票情况分析",
        "icon": "ticket",
        "theme_code": "ticket",
        "question": "【主题解读】票务",
    },
    {
        "id": "public_opinion",
        "title": "舆情",
        "subtitle": "口碑监测与负面预警",
        "icon": "alert",
        "theme_code": "public_opinion",
        "question": "【主题解读】舆情",
    },
    {
        "id": "ota",
        "title": "OTA直销",
        "subtitle": "直销渠道核销与订单分析",
        "icon": "globe",
        "theme_code": "ota_direct_sales",
        "question": "【主题解读】OTA直销",
    },
]

# 主题中文名 → 供试试问我/选择器按主题筛选
THEME_NAMES: dict[str, str] = {
    "passenger_flow": "客流",
    "ticket": "票务",
    "public_opinion": "舆情",
    "ota_direct_sales": "OTA直销",
}


# ────────────────────────────────────────────────────────────────
# 试试问我问题列表（需求文档 3.2.1.2，共 18 条）
# ────────────────────────────────────────────────────────────────

GUIDE_QUESTIONS: list[dict] = [
    # 客流主题（5 条）
    {"id": "gq_001", "theme_code": "passenger_flow", "question": "今日实时在园人数是多少？与昨日同期相比如何？"},
    {"id": "gq_002", "theme_code": "passenger_flow", "question": "本周客流高峰出现在哪几天、哪个时段？"},
    {"id": "gq_003", "theme_code": "passenger_flow", "question": "当前游客中男女比例和年龄结构是怎样的？"},
    {"id": "gq_004", "theme_code": "passenger_flow", "question": "今天哪个入口进园人数最多？"},
    {"id": "gq_005", "theme_code": "passenger_flow", "question": "近7天客流趋势如何？与上周环比变化？"},
    # 票务主题（5 条）
    {"id": "gq_006", "theme_code": "ticket", "question": "今日售票收入及与昨日对比情况？"},
    {"id": "gq_007", "theme_code": "ticket", "question": "各票种销售占比是多少？哪种票最畅销？"},
    {"id": "gq_008", "theme_code": "ticket", "question": "线上渠道和线下窗口售票比例如何？"},
    {"id": "gq_009", "theme_code": "ticket", "question": "今日退票量有多少？退票率是否异常？"},
    {"id": "gq_010", "theme_code": "ticket", "question": "哪个OTA直销渠道今日贡献最多售票量？"},
    # 舆情主题（4 条）
    {"id": "gq_011", "theme_code": "public_opinion", "question": "今日舆情情感倾向如何？正面/负面占比？"},
    {"id": "gq_012", "theme_code": "public_opinion", "question": "当前有没有突发热点事件需要关注？"},
    {"id": "gq_013", "theme_code": "public_opinion", "question": "游客最常提及的痛点是什么？"},
    {"id": "gq_014", "theme_code": "public_opinion", "question": "哪个平台负面评价最多？"},
    # OTA直销主题（4 条）
    {"id": "gq_015", "theme_code": "ota_direct_sales", "question": "今日各渠道核销率如何？"},
    {"id": "gq_016", "theme_code": "ota_direct_sales", "question": "本周各渠道订单情况？"},
    {"id": "gq_017", "theme_code": "ota_direct_sales", "question": "各渠道客单价是多少？"},
    {"id": "gq_018", "theme_code": "ota_direct_sales", "question": "各渠道总出票率分别是多少？"},
]


def list_guide_questions(*, theme_code: str | None = None, page: int = 1, page_size: int = 5) -> tuple[list[dict], int]:
    """试试问我问题列表（按主题筛选 + 分页）。返回 (items, total)。"""
    items = [q for q in GUIDE_QUESTIONS if theme_code is None or q["theme_code"] == theme_code]
    total = len(items)
    start = (page - 1) * page_size
    return items[start:start + page_size], total


# ────────────────────────────────────────────────────────────────
# 三级平铺选择器（主题 → 指标 → 问题）
# 基于 18 条预设问题按指标语义分组；需求文档标注"完整三级数据映射（待补充）"。
# ────────────────────────────────────────────────────────────────

SELECTOR_DATA: list[dict] = [
    {
        "theme": "客流",
        "metrics": [
            {"name": "实时客流", "questions": [
                "今日实时在园人数是多少？与昨日同期相比如何？",
            ]},
            {"name": "客流高峰", "questions": [
                "本周客流高峰出现在哪几天、哪个时段？",
            ]},
            {"name": "游客结构", "questions": [
                "当前游客中男女比例和年龄结构是怎样的？",
            ]},
            {"name": "入口客流", "questions": [
                "今天哪个入口进园人数最多？",
            ]},
            {"name": "客流趋势", "questions": [
                "近7天客流趋势如何？与上周环比变化？",
            ]},
        ],
    },
    {
        "theme": "票务",
        "metrics": [
            {"name": "售票收入", "questions": [
                "今日售票收入及与昨日对比情况？",
            ]},
            {"name": "票种结构", "questions": [
                "各票种销售占比是多少？哪种票最畅销？",
            ]},
            {"name": "渠道占比", "questions": [
                "线上渠道和线下窗口售票比例如何？",
            ]},
            {"name": "退票情况", "questions": [
                "今日退票量有多少？退票率是否异常？",
            ]},
            {"name": "渠道贡献", "questions": [
                "哪个OTA直销渠道今日贡献最多售票量？",
            ]},
        ],
    },
    {
        "theme": "舆情",
        "metrics": [
            {"name": "情感倾向", "questions": [
                "今日舆情情感倾向如何？正面/负面占比？",
            ]},
            {"name": "热点事件", "questions": [
                "当前有没有突发热点事件需要关注？",
            ]},
            {"name": "游客痛点", "questions": [
                "游客最常提及的痛点是什么？",
            ]},
            {"name": "平台分布", "questions": [
                "哪个平台负面评价最多？",
            ]},
        ],
    },
    {
        "theme": "OTA直销",
        "metrics": [
            {"name": "核销率", "questions": [
                "今日各渠道核销率如何？",
            ]},
            {"name": "订单情况", "questions": [
                "本周各渠道订单情况？",
            ]},
            {"name": "客单价", "questions": [
                "各渠道客单价是多少？",
            ]},
            {"name": "出票率", "questions": [
                "各渠道总出票率分别是多少？",
            ]},
        ],
    },
]
