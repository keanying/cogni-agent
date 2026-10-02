"""prewarm:固定问题预热一次性扫描进程(20 号 §六 + 22 号复盘, K8s CronJob 调起)。

    python -m cogni_agent.cronjobs.prewarm [--dry-run]

无等待状态机——单次运行按下列顺序执行后秒级退出,不轮询等待任何任务
(任务执行发生在各轮扫描之间,本轮扫描到的终态任务都是上一轮派出的):

核心语义(22 号复盘定稿:景区×问题×业务日 每组合每天一条,当日窗口重试):
- 业务日 biz_date=任务创建日(东八区),time_period 仅作 LLM 参数透传、与 biz_date 解耦;
- 报告任务初派即建当日缓存占位行(answer=NULL 锁位);任务 COMPLETED 收割时 UPDATE 补全;
- 补派/失败登记只处理「今天创建」的任务 → 昨天完成/没完成的任务一律不再补派,
  昨天失败清零,业务日翻页(北京日 00:00)即重新初派、重新有 5 次尝试(跨天即断);
- 报告环节与摘要环节各自当日最多尝试 _MAX_DISPATCH_RETRY / _MAX_ENRICH_RETRY 次;
- 消费端只认当日行(biz_date=今天 且 answer 非空),今天未出=空态,绝不回退昨日;
- 历史行保留 _RETENTION_DAYS 天后裁剪。

单轮顺序:
  ⑥ 裁剪: 清 biz_date < 今天-7 的旧行(防表膨胀,历史留存 7 天)。
  ⑤ 补派-失败报告: 今日创建的 redispatch 报告任务 → 重新 create_task(沿用原 payload,
     数据口径不漂移;旧任务标记 replaced_by 防重复补派;dispatch_attempt 沿当日链继承)。
  ① 收割-报告: 当日创建的 COMPLETED 报告任务 → 按 (question_id, scenic_id, biz_date)
     upsert 当日缓存行(占位行 UPDATE 补全 answer/structured;无占位则 INSERT)。
     跨天缝隙完成的昨日任务不落行(消费只认当日行,收了也无价值)。
  ① 收割-摘要: COMPLETED 摘要任务 → UPDATE summary/metric,清 enrich_task_id。
  ② 派单-报告: 问题表 prewarm_enabled → 对每个在用景区按业务日判重(当日占位行
     已存在即跳过,调度无「每日 N 点」时点概念)→ create_task + 建当日占位行。
     行维度含 ota_account(25 号):
     OTA 主题与概览按「景区×账号」展开(scenic_ota_accounts 账号并集去重,任务
     question_params 注入 account——模型调 OTA指标 从槽位取到,不再传空),概览额外
     保留一行账号无关基础版(NULL,未绑用户回退);景区无任何有效账号时 OTA 主题
     整个跳过(不派 LLM、不落行,零 token)。
  ③ 派单-摘要(24号收敛:仅主题解读行): 当日行 answer 非空、无 summary、无 enrich_task_id、
     且该行摘要重试未达上限 → create_task(mode=enrich, executor=摘要技能直达, message=answer)。
     摘要消费方只有 home 主题卡副标题/insight.sentence(均读【主题解读】行),其余预热行不派。
  ④ 登记失败(只处理今日创建任务,补派统一在下一轮):报告 FAILED → dispatch_attempt+1
     写回任务自身 payload 并置 redispatch(达上限 ERROR 停止);摘要 FAILED →
     enrich_attempt+1 挂该行溯源报告任务 payload 并解除行占位(补派落下一轮 ③)。

幂等:任意一步失败不影响其余;下一轮 CronJob 扫描天然续跑。失败登记/补派严格限定
当日创建任务(biz_date 口径一致),昨日遗留任务不补、次日重新初派。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import re

from sqlalchemy import delete, select, update

from cogni_agent.common.db import get_sessionmaker
from cogni_agent.common.logging import get_logger
from cogni_agent.common.time import now_cst, to_cst, today_cst, today_start_cst
from cogni_agent.config import get_settings

log = get_logger("prewarm")

# ACTIVE_TASK_STATUSES 与 control_plane 一致(不 import 常量避免耦合)
_ACTIVE = ("CREATED", "QUEUED", "RUNNING", "WAITING_HUMAN")

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL)

# 同一缓存行的摘要补丁最多自动补派次数(失败后下一轮 ③ 重派,次数记于该行
# 溯源报告任务的 payload.enrich_attempt),达上限后停止补派并 ERROR 暴露。
_MAX_ENRICH_RETRY = 5

# 报告任务(问题×景区)执行失败后的链式自动补派上限(次数记于任务自身
# payload.dispatch_attempt,沿当日补派链继承,达上限后不再补派并 ERROR 暴露)。
# 作用域=当日:初派 payload 不带 attempt → 每天从 0 计,昨日链不延续。
_MAX_DISPATCH_RETRY = 5


def _prewarm_token_budget() -> int:
    """预热任务的单任务 token 预算(覆盖 runtime.guardrails.default_task_token_budget)。

    默认 50000 按「累计 prompt+completion」判定,概览类技能工具多、每轮上下文大,
    实测当日 25/25 全熔断;离线预热不占在线问答的护栏额度,单独放宽。
    """
    return int(get_settings().get("runtime", "guardrails", "prewarm_task_token_budget", default=150000) or 150000)

# 历史缓存行保留天数(按 biz_date),超期由本轮扫描开头裁剪。
_RETENTION_DAYS = 7

# OTA 主题(问题库 app_question_bank.theme_code):该主题按「景区×账号」拆行(25 号),
# 预热侧 scenic_ota_accounts 展开账号、无有效账号景区整个跳过(不派 LLM、不落行,
# 零 token);APP 直出侧 user_ota_accounts 用户级判定,当前用户未绑返回「未配置账号」
# 无数据说明。
_OTA_THEME = "ota_direct_sales"


def _today() -> dt.date:
    """业务日基准=北京日期(容器时区为 UTC,date.today() 在 00:00~08:00 会偏一天)。"""
    return today_cst()


def _today_start() -> dt.datetime:
    """北京当日 00:00(aware,可直接与 TIMESTAMPTZ 列比较)。"""
    return today_start_cst()


def _biz_date_of(payload: dict, created_at: dt.datetime) -> dt.date:
    """任务业务日:payload.biz_date 优先,缺失(老任务)回退创建日(统一北京时间)。"""
    raw = payload.get("biz_date")
    if isinstance(raw, str) and raw:
        try:
            return dt.date.fromisoformat(raw)
        except ValueError:
            pass
    return to_cst(created_at or now_cst()).date()


async def run(dry_run: bool = False) -> None:
    from cogni_agent.control_plane_service import queue as taskq
    from cogni_agent.gateway_service.models import AppPrewarmCache, AppQuestionBank
    from cogni_agent.gateway_service.ota_accounts import scenic_ota_accounts
    from cogni_agent.tool_center_service.skill.models import Skill

    async with get_sessionmaker()() as db:
        now = now_cst()
        today = _today()
        today_start = _today_start()
        # 收割只认当日任务(24 号口径):消费端只读当日行,昨日行收了也无消费
        # 价值(昨日空态已定格,业务日翻页即重新初派);跨天缝隙里完成的昨日任务
        # 直接不落行,省掉无效写。补派/登记/派单同口径严格当日。
        harvest_since = today_start
        harvested = 0
        dispatched = 0

        # ── ⑥ 裁剪:历史行保留 _RETENTION_DAYS 天(dry-run 跳过——只读观察) ──
        purged = 0
        if not dry_run:
            cutoff = today - dt.timedelta(days=_RETENTION_DAYS)
            purged = (await db.execute(delete(AppPrewarmCache).where(
                AppPrewarmCache.biz_date < cutoff))).rowcount
            if purged:
                log.info(f"裁剪 {purged} 条 biz_date < {cutoff} 的历史缓存行")

        # 终态任务快照:任务执行发生在各轮扫描之间,本 run 内该集合不变。
        done_completed = (await db.execute(
            select(taskq.Task).where(
                taskq.Task.source == "prewarm",
                taskq.Task.status == "COMPLETED",
                taskq.Task.created_at >= harvest_since,
            ).order_by(taskq.Task.created_at).limit(300)
        )).scalars().all()
        # 失败登记/补派只处理今天创建的任务 → 昨日失败清零、不再补派。
        done_failed = (await db.execute(
            select(taskq.Task).where(
                taskq.Task.source == "prewarm",
                taskq.Task.status == "FAILED",
                taskq.Task.created_at >= today_start,
            ).order_by(taskq.Task.created_at).limit(200)
        )).scalars().all()

        # ── ⑤ 补派-失败报告(先于一切:处理上一轮 ④ 登记的当日 redispatch 任务) ──
        # done_failed 已限「今日创建」→ 昨日任务不在集合,补派链天然断在昨天;
        # 今日初派任务不带旧链 attempt → 当天从 0 计。
        for t in done_failed:
            p = t.payload or {}
            if p.get("mode") == "enrich" or not p.get("redispatch"):
                continue
            qid = p.get("question_id") or ""
            attempt = p.get("dispatch_attempt") or 0
            scenic = t.scenic_id or ""
            if attempt >= _MAX_DISPATCH_RETRY or not scenic:
                log.error(f"报告任务 {t.id[:8]} 需补派但 attempt={attempt} 已达上限"
                          f"或景区为空,清除补派标记不再重派"
                          f"(question={qid[:8]}@{scenic or '(空景区)'})")
                await db.execute(update(taskq.Task).where(
                    taskq.Task.id == t.id).values(payload={**p, "redispatch": False}))
                continue
            if dry_run:
                log.info(f"[dry] 补派失败报告: {qid[:8]} @ {scenic} "
                         f"(attempt={attempt}/{_MAX_DISPATCH_RETRY})")
                continue
            # 复用原 payload(含 question_params/executor_version_id,数据口径不漂移),
            # executor 字段从旧任务复制(技能版本随旧任务固化,避免版本漂移)。
            np = {k: v for k, v in p.items()
                  if k not in ("redispatch", "replaced_by")}
            ti = await taskq.create_task(
                scenic_id=scenic, source="prewarm", priority=8, payload=np,
                executor_type=t.executor_type, executor_id=t.executor_id,
                conversation_id=None, user_id=t.user_id)
            pp = dict(p)
            pp["redispatch"] = False
            pp["replaced_by"] = ti["id"]
            await db.execute(update(taskq.Task).where(
                taskq.Task.id == t.id).values(payload=pp))
            dispatched += 1
            log.info(f"补派失败报告 {ti['id'][:8]} → 替代 {t.id[:8]}"
                     f"(question={qid[:8]}@{scenic}, attempt={attempt})")

        # ── ① 收割 COMPLETED 报告任务 → upsert 业务日缓存行 ──
        # 初派时已建当日占位行(answer=NULL) → 收割 = UPDATE 补全(修复旧版
        # exists→skip 永不刷新的坑);异常缺行(老任务/行被运营删)则 INSERT。
        # task_id 保持初派任务(摘要重试计数挂其 payload,溯源链稳定)。
        for t in done_completed:
            if dry_run:
                continue  # dry-run 不收割不写库,留给下一轮真实扫描(幂等可续)
            p = t.payload or {}
            qid = p.get("question_id")
            if not qid or p.get("mode") == "enrich":
                continue
            scenic = t.scenic_id or ""
            if not scenic:
                continue  # 无景区行无消费价值(旧数据兜底,新派单只对绑定景区)
            bdate = _biz_date_of(p, t.created_at)
            result = t.result or {}
            if not result.get("answer"):
                continue
            # 27 号:版本维度——任务钉的技能版本号(从 payload.executor_version_id
            # 反查;查不到=旧任务,行版本 NULL,消费严格匹配天然忽略,自然淘汰)。
            _sv = None
            if p.get("executor_version_id"):
                from cogni_agent.tool_center_service.versioning import get_version as _gv
                _vrow = await _gv(db, p["executor_version_id"])
                _sv = _vrow.version if _vrow is not None else None
            row = (await db.execute(select(AppPrewarmCache).where(
                AppPrewarmCache.question_id == qid,
                AppPrewarmCache.scenic_id == scenic,
                AppPrewarmCache.ota_account == p.get("ota_account"),
                AppPrewarmCache.skill_version == _sv,
                AppPrewarmCache.biz_date == bdate))).scalar_one_or_none()
            if row is None:
                db.add(AppPrewarmCache(
                    question_id=qid, scenic_id=scenic, biz_date=bdate,
                    ota_account=p.get("ota_account"), skill_version=_sv,
                    task_id=t.id, answer=result["answer"],
                    structured=(result.get("structured") or None),
                    data_as_of=(t.updated_at or now),
                ))
            else:
                await db.execute(update(AppPrewarmCache).where(
                    AppPrewarmCache.id == row.id).values(
                    answer=result["answer"],
                    structured=(result.get("structured") or None),
                    data_as_of=(t.updated_at or now)))
            harvested += 1
            log.info(f"收割报告任务 {t.id[:8]} → 缓存 {bdate}"
                     f"(question={qid[:8]}@{scenic})")

        # ── ① 收割 COMPLETED 摘要任务 → 成功写 summary / 产出不合法按失败计数 ──
        # 产出不合法(无 ```json 块 / json 解析失败 / summary 空)在语义上等于本次尝试失败:
        # 不能只清占位不计数——否则下一轮 ③ 条件仍满足(answer 非空/summary 空/无占位),
        # 每小时重派却永远 enrich_attempt=0,5 次上限永不触发 → 无限空转、summary 永久空
        # (dev「命中缓存但 sentence 空」即此路径)。故此处与 ④ FAILED 登记同一口径:
        # 计数挂该行溯源报告任务 payload.enrich_attempt,达上限 ERROR 停止,占位解除待下轮补派。
        for t in done_completed:
            if dry_run:
                continue  # dry-run 不收割不写库
            p = t.payload or {}
            if p.get("mode") != "enrich":
                continue
            row = (await db.execute(select(AppPrewarmCache).where(
                AppPrewarmCache.enrich_task_id == t.id))).scalar_one_or_none()
            if row is None:
                continue
            answer = (t.result or {}).get("answer") or ""
            m = _JSON_BLOCK_RE.search(answer)
            summary, metric, items = None, None, None
            if m:
                try:
                    obj = json.loads(m.group(1))
                    summary = (obj.get("summary") or "")[:128] or None
                    metric = obj.get("metric") if isinstance(obj.get("metric"), dict) else None
                    # 24号四维指标:摘要技能新增输出 metrics 数组(概览报告四域齐全),
                    # 收敛成 {theme: {value, display}} 随行存 overview_metrics.items,
                    # panel/insight 与 agent/home 彩色指标条据此读真数据(替换样例)。
                    raw = obj.get("metrics")
                    if isinstance(raw, list):
                        items = {it.get("theme"): {"value": it.get("value"),
                                                    "display": it.get("display")}
                                 for it in raw
                                 if isinstance(it, dict) and it.get("theme")}
                except (json.JSONDecodeError, ValueError):
                    pass
            if summary:
                om = dict(metric) if metric else {}
                if items:
                    om["items"] = items
                await db.execute(
                    update(AppPrewarmCache).where(AppPrewarmCache.id == row.id).values(
                        summary=summary,
                        overview_metrics=(om or None),
                        enrich_task_id=None))
                harvested += 1
                log.info(f"收割摘要任务 {t.id[:8]} → 行 {row.id[:8]} (summary=有)")
                continue
            # 产出不合法:计一次失败(与 ④ 摘要 FAILED 同挂载点,次数当日累计)
            parent = (await db.execute(select(taskq.Task).where(
                taskq.Task.id == row.task_id))).scalar_one_or_none()
            attempt = ((parent.payload or {}).get("enrich_attempt")
                       if parent is not None else 0) or 0
            new_attempt = attempt + 1
            if parent is not None:
                pp = dict(parent.payload or {})
                pp["enrich_attempt"] = new_attempt
                await db.execute(update(taskq.Task).where(
                    taskq.Task.id == parent.id).values(payload=pp))
            await db.execute(update(AppPrewarmCache).where(
                AppPrewarmCache.id == row.id).values(enrich_task_id=None))
            if new_attempt >= _MAX_ENRICH_RETRY:
                log.error(f"摘要任务 {t.id[:8]} 产出不合法(COMPLETED 但无合法 json/summary)"
                          f"当日累计 {new_attempt} 次达上限({_MAX_ENRICH_RETRY}),行 {row.id[:8]}"
                          f"(question={row.question_id[:8]}@{row.scenic_id},biz_date={row.biz_date})"
                          f"停止自动补派,该行当日摘要缺失,需人工介入")
            else:
                log.warning(f"摘要任务 {t.id[:8]} 产出不合法(无合法 json/summary),行 {row.id[:8]}"
                            f"已登记第 {new_attempt}/{_MAX_ENRICH_RETRY} 次失败,"
                            f"下一轮扫描自动补派摘要")

        # ── ② 派单-报告(到点问题) ────────────────────────────────
        # 摘要技能(直达执行体)
        sum_skill = (await db.execute(select(Skill).where(
            Skill.name == "数字大脑-预热摘要"))).scalar_one_or_none()
        # 预热景区范围 = 在用用户绑定的景区(usr_users 去重)。不以景区主数据
        # (sys_scenics)为源:平台景区主数据达万级,问题×景区任务量不可行;只有
        # 被在用运营账号绑定的景区才是 APP 端真正会消费预热缓存的维度。
        from cogni_agent.user_center_service.models import User
        prewarm_scenics = list((await db.execute(
            select(User.scenic_id)
            .where(User.scenic_id.isnot(None), User.scenic_id != "",
                   User.is_active.is_(True))
            .distinct())).scalars())
        due = (await db.execute(select(AppQuestionBank).where(
            AppQuestionBank.prewarm_enabled.is_(True),
            AppQuestionBank.active.is_(True),
            AppQuestionBank.skill_id.isnot(None),
            # 注:调度口径=业务日,不再看 next_run_at 时点(无「每日 N 点」概念)。
            # 每轮扫描全部启用预热的问题,是否真派单由「当日占位行是否已存在」判定
            # (见下方 exists 检查,维度含 biz_date),行缺失即补派、可自愈。
        ))).scalars().all()
        if due and not prewarm_scenics:
            log.warning(f"{len(due)} 个预热问题待派,但在用用户未绑定任何景区,本轮跳过派单")
        for q in due:
            # 27 号:版本随指派(fail-closed)——问题行必须配 agent_id 作为版本锚;
            # 技能版本 = 景区用户对该锚 agent 指派快照里该技能的 pin(去重集合,
            # 每版本一行);无锚/无指派 → 该问题不可用,跳过派单。
            # **绝不回退「最新 published」**——版本体系的意义就是可指定。
            anchor_agent = q.agent_id
            if not anchor_agent:
                log.warning(f"问题「{q.question_text[:20]}」未配置 agent_id(版本锚),"
                            f"跳过派单(27 号:版本随指派,无锚不派)")
                continue
            from cogni_agent.tool_center_service.versioning import (
                published_version_by_no, scenic_pinned_skill_versions,
            )
            base_payload = {
                "message": q.question_text, "question_id": q.id,
                "question_source": "prewarm",
                "biz_date": today.isoformat(),  # 业务日=任务创建日(东八区)
                "user_id": None, "role": "scenic_operator",
                "question_params": _expand_tpl(q.param_template),
                # 离线预热单独放宽 token 预算(概览等多工具技能按默认 50000 必熔断)
                "overrides": {"token_budget": _prewarm_token_budget()},
            }
            is_ota = (q.theme_code or "") == _OTA_THEME
            # 25 号:OTA 数据按「景区×账号」拆分——OTA 主题与概览(报告调 OTA指标,
            # 数据随账号变)按景区账号展开;其余主题数据与账号无关,一景区一行。
            # 概览额外保留一行账号无关基础版(NULL):未绑账号用户回退看三域概览。
            is_overview = (q.theme_code or "") == "overview"
            acct_split = is_ota or is_overview
            # 景区 × (版本 pin) × (账号) × 问题 × 业务日 派单:27 号加版本维度
            # (景区用户指派一致=常态单版本;版本分流各一行)。缓存行按
            # (question_id, scenic_id, ota_account, skill_version, biz_date) 唯一,
            # APP 端按「用户景区×用户账号×指派版本」当日命中直出(全部严格)。
            for scenic_id in prewarm_scenics:
                pins = await scenic_pinned_skill_versions(db, anchor_agent, scenic_id, q.skill_id)
                if not pins:
                    log.info(f"问题「{q.question_text[:20]}」@{scenic_id} 无该 agent 指派"
                             f"(不可用),跳过派单 (biz_date={today})")
                    continue
                # 展开维度:账号拆分主题=该景区全部账号(+概览的 NULL 基础行);
                # 其余主题=[None](账号无关行)。
                if acct_split:
                    accounts = await scenic_ota_accounts(db, scenic_id)
                    if is_ota and not accounts:
                        # OTA 主题且景区无任何有效账号:不派 LLM、不落行(零 token)。
                        log.info(f"OTA 问题「{q.question_text[:20]}」@{scenic_id} 无有效 OTA 账号,"
                                 f"跳过派单(零 token) (biz_date={today})")
                        continue
                    # 概览:账号行 + NULL 基础行(未绑用户回退);OTA 主题:仅账号行
                    dims = accounts + ([None] if is_overview else [])
                else:
                    dims = [None]
                import copy

                for pin in sorted(pins):
                    pub = await published_version_by_no(db, "skill", q.skill_id, pin)
                    if pub is None:
                        log.warning(f"问题 {q.id} 技能 {q.skill_id[:8]} 指派 pin v{pin}"
                                    f"无 published 版本行,跳过该版本")
                        continue
                    payload = {**base_payload, "executor_version_id": pub.id}
                    for account in dims:
                        # payload 按维度克隆:账号维度注入 ota_account + question_params.account
                        # (执行器槽位注入,模型调 OTA指标 时从会话上下文取到,不再传空)
                        if account:
                            _payload = copy.deepcopy(payload)
                            _payload["ota_account"] = account
                            _payload["question_params"] = {**(payload.get("question_params") or {}),
                                                           "account": account}
                        else:
                            _payload = payload
                        if dry_run:
                            log.info(f"[dry] 派单报告: {q.question_text[:20]} @ {scenic_id}"
                                     f"{' acct=' + account if account else ''} v{pin}"
                                     f" (biz_date={today})")
                            continue  # dry-run 不建行不建任务
                        # 当日占位行已存在 = 今日已初派(上轮半程/并发),跳过防重复派单
                        # (账号×版本维度:各行各占各的位,表达式唯一索引兜底)
                        exists = (await db.execute(select(AppPrewarmCache.id).where(
                            AppPrewarmCache.question_id == q.id,
                            AppPrewarmCache.scenic_id == scenic_id,
                            AppPrewarmCache.ota_account == account,
                            AppPrewarmCache.skill_version == pin,
                            AppPrewarmCache.biz_date == today))).scalar_one_or_none()
                        if exists:
                            continue
                        ti = await taskq.create_task(
                            scenic_id=scenic_id, source="prewarm", priority=8,
                            payload=_payload,
                            executor_type="skill", executor_id=q.skill_id,
                            conversation_id=None, user_id=None)
                        # 初派即建当日占位行(answer=NULL 锁位,收割时 UPDATE 补全)——
                        # 保证「每天每问题(×账号×版本)必有一条当日记录」,失败未出=空态
                        # 由消费端识别(27 号:行带 skill_version,消费严格匹配)。
                        db.add(AppPrewarmCache(
                            question_id=q.id, scenic_id=scenic_id,
                            ota_account=account, skill_version=pin,
                            biz_date=today, task_id=ti["id"], answer=None, structured=None,
                            data_as_of=now))
                        log.info(f"派单报告 {ti['id'][:8]}: {q.question_text[:24]} "
                                 f"@ {scenic_id}{f' acct={account}' if account else ''} "
                                 f"v{pin} (biz_date={today})")
                        dispatched += 1
            if dry_run:
                continue

        # ── ③ 派单-摘要(主题解读行才派,24号决策点3) ──────────────────
        # 只扫当日行:报告收割(answer 非空)后同行即可派;摘要失败后占位在 ④ 解除,
        # 补派落下一轮 ③(隔一轮再补,不在同一轮"刚失败立刻又派")。
        # 24号收敛:摘要(summary/overview_metrics)消费方 = home 5 张主题卡副标题 +
        # insight.sentence,均只读「【主题解读】」行 → 只对主题解读行派摘要;
        # 其余预热行(昨日指标类直出行)无摘要消费方,不派(省 LLM 调用)。
        if sum_skill is not None:
            from cogni_agent.tool_center_service.versioning import latest_published_version
            sum_pub = await latest_published_version(db, "skill", sum_skill.id)
            theme_qids = select(AppQuestionBank.id).where(
                AppQuestionBank.question_text.like("【主题解读】%"))
            need = (await db.execute(select(AppPrewarmCache).where(
                AppPrewarmCache.biz_date == today,
                AppPrewarmCache.answer.isnot(None),
                AppPrewarmCache.summary.is_(None),
                AppPrewarmCache.enrich_task_id.is_(None),
                AppPrewarmCache.scenic_id.isnot(None),
                AppPrewarmCache.scenic_id != "",
                AppPrewarmCache.question_id.in_(theme_qids),
            ))).scalars().all()
            for row in need:
                if dry_run:
                    log.info(f"[dry] 派单摘要: 行 {row.id[:8]} (biz_date={today})")
                    continue
                if sum_pub is None:
                    continue
                # 重试上限门禁:该行摘要失败累计已达上限的行不再自动派(错误已在
                # ④ FAILED 登记时 ERROR 暴露),避免达上限后每轮 ③ 又空转重派。
                parent = (await db.execute(select(taskq.Task).where(
                    taskq.Task.id == row.task_id))).scalar_one_or_none()
                attempt = ((parent.payload or {}).get("enrich_attempt")
                           if parent is not None else 0) or 0
                if attempt >= _MAX_ENRICH_RETRY:
                    log.warning(f"行 {row.id[:8]} 摘要重试已达上限({attempt}),不再自动派单")
                    continue
                ti = await taskq.create_task(
                    scenic_id=row.scenic_id or None, source="prewarm", priority=8,
                    payload={"mode": "enrich", "question_id": row.question_id,
                             "message": row.answer[:12000], "question_source": "prewarm",
                             "biz_date": row.biz_date.isoformat(),
                             "user_id": None, "role": "scenic_operator",
                             "executor_version_id": sum_pub.id,
                             # 对齐 S7 直跳:overrides.skill_ids 使 resolver 精确匹配单技能,
                             # 跳过 Triager 意图分类(否则摘要报告文本会被 Triager 判澄清反问)
                             "overrides": {"skill_ids": [sum_skill.id],
                                           "token_budget": _prewarm_token_budget()}},
                    executor_type="skill", executor_id=sum_skill.id,
                    conversation_id=None, user_id=None)
                await db.execute(update(AppPrewarmCache).where(
                    AppPrewarmCache.id == row.id).values(enrich_task_id=ti["id"]))
                log.info(f"派单摘要 {ti['id'][:8]} → 行 {row.id[:8]} (biz_date={today})")
                dispatched += 1

        # ── ④ 登记失败(只处理今日任务;本轮只登记不补派,补派在下一轮扫描) ──
        # 报告 FAILED → 链式计数 dispatch_attempt 写回任务自身 payload,置
        # redispatch=true 待下一轮 ⑤ 补派;达上限停止并 ERROR。空景区任务无
        # 消费价值不登记补派(直接 ERROR,提示人工清理异常任务)。
        for t in done_failed:
            if dry_run:
                continue  # dry-run 不登记不写库
            p = t.payload or {}
            if p.get("mode") == "enrich":
                continue
            qid = p.get("question_id")
            if not qid:
                continue
            # 已被补派链替代(replaced_by)或已登记待补(redispatch)的任务跳过,
            # 防止同一失败被重复登记计数。
            if p.get("replaced_by") or p.get("redispatch"):
                continue
            err = (t.result or {}).get("error") or "未知错误"
            if not t.scenic_id:
                log.error(f"预热报告任务 {t.id[:8]} FAILED(question={qid[:8]}@空景区):"
                          f"{err[:200]} —— 空景区无消费价值不自动补派,需人工处理")
                continue
            attempt = (p.get("dispatch_attempt") or 0) + 1
            np = dict(p)
            np["dispatch_attempt"] = attempt
            np["redispatch"] = attempt < _MAX_DISPATCH_RETRY
            await db.execute(update(taskq.Task).where(
                taskq.Task.id == t.id).values(payload=np))
            if attempt >= _MAX_DISPATCH_RETRY:
                log.error(f"预热报告任务 {t.id[:8]} FAILED 当日累计 {attempt} 次达上限"
                          f"({_MAX_DISPATCH_RETRY})(question={qid[:8]}@{t.scenic_id},"
                          f"biz_date={_biz_date_of(p, t.created_at)}),"
                          f"停止自动补派,该组合当日预热结果缺失需人工介入。原因: {err[:200]}")
            else:
                log.warning(f"报告任务 {t.id[:8]} FAILED(question={qid[:8]}@{t.scenic_id}),"
                            f"已登记第 {attempt}/{_MAX_DISPATCH_RETRY} 次失败,"
                            f"下一轮扫描自动补派。原因: {err[:200]}")

        # 摘要 FAILED → 计数挂该行溯源报告任务的 payload.enrich_attempt(行 task_id=
        # 当日初派报告任务,1:1 天然挂载,免迁移)并解除行占位。
        # 占位清除发生在 ③ 之后 → 补派落在下一轮 ③,即"失败本轮不重派,下轮补"。
        for t in done_failed:
            if dry_run:
                continue  # dry-run 不登记不写库
            p = t.payload or {}
            if p.get("mode") != "enrich":
                continue
            row = (await db.execute(select(AppPrewarmCache).where(
                AppPrewarmCache.enrich_task_id == t.id))).scalar_one_or_none()
            if row is None:
                continue  # 占位已被处理/转移,该失败已登记过
            parent = (await db.execute(select(taskq.Task).where(
                taskq.Task.id == row.task_id))).scalar_one_or_none()
            attempt = ((parent.payload or {}).get("enrich_attempt")
                       if parent is not None else 0) or 0
            new_attempt = attempt + 1
            if parent is not None:
                pp = dict(parent.payload or {})
                pp["enrich_attempt"] = new_attempt
                await db.execute(update(taskq.Task).where(
                    taskq.Task.id == parent.id).values(payload=pp))
            await db.execute(update(AppPrewarmCache).where(
                AppPrewarmCache.id == row.id).values(enrich_task_id=None))
            err = (t.result or {}).get("error") or "未知错误"
            if new_attempt >= _MAX_ENRICH_RETRY:
                log.error(f"摘要任务 {t.id[:8]} FAILED 当日累计 {new_attempt} 次达上限"
                          f"({_MAX_ENRICH_RETRY}),行 {row.id[:8]}"
                          f"(question={row.question_id[:8]}@{row.scenic_id},"
                          f"biz_date={row.biz_date}) "
                          f"停止自动补派,该行当日摘要缺失,需人工介入。原因: {err[:200]}")
            else:
                log.warning(f"摘要任务 {t.id[:8]} FAILED,行 {row.id[:8]} 已登记第 "
                            f"{new_attempt}/{_MAX_ENRICH_RETRY} 次失败,下一轮扫描"
                            f"自动补派摘要。原因: {err[:200]}")

        await db.commit()
        log.info(f"prewarm 完成: 收割 {harvested}, 派单 {dispatched}, 裁剪 {purged}")


def _expand_tpl(template: dict | None) -> dict:
    """展开参数模板占位符(复用 question_bank 的展开器)。"""
    from cogni_agent.gateway_service.question_bank import expand_param_template
    return expand_param_template(template)


def main() -> None:
    ap = argparse.ArgumentParser(description="固定问题预热扫描(一次性,CronJob 调起)")
    ap.add_argument("--dry-run", action="store_true", help="只打印动作不执行")
    args = ap.parse_args()
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
