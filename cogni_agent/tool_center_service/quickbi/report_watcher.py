"""小Q报告后台续跑。

报告生成动辄几分钟到几十分钟,而发起它的那条 HTTP 连接活不了那么久 ——
页面刷新、切走、网络抖一下,跟踪就断了。断了之后运行记录停在 running:
远端其实已经生成完了,库里却永远是「生成中」,没人去收。

所以收结果这件事不能挂在客户端身上。这个模块是服务端的常驻轮询:
只要库里还有 status=running 的报告,就一直去拉,直到拿到结论(成功/失败)
或者超过兜底时限。客户端在不在线,与结果能不能落库无关。

设计取舍:
- **无状态**:每轮从库里查,不在内存里攒待办 —— 进程重启后自动接着收,
  多副本同时跑也只是重复拉取(refresh_report 本身幂等),不会写坏数据;
- **失败不中断循环**:单条报告出错只记日志跳过,下一轮再试。一条坏记录
  不该让其余报告都收不回来;
- **有兜底时限**:超过 report_give_up_hours 仍没结论的,标记失败并写明原因。
  永远 running 的记录比失败更难排查 —— 没人知道它是在跑还是已经死了。
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from cogni_agent.common.db import get_sessionmaker, utcnow
from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings
from cogni_agent.tool_center_service.quickbi.errors import QuickBIError
from cogni_agent.tool_center_service.quickbi.models import QbiRun

log = get_logger("quickbi.report_watcher")


def _setting(key: str, default):
    return (get_settings().get("quickbi", default={}) or {}).get(key, default)


async def _pending_runs() -> list[QbiRun]:
    """还没有结论的报告。按创建时间正序 —— 先发起的先收,免得新报告把老的饿着。"""
    async with get_sessionmaker()() as db:
        rows = await db.execute(
            select(QbiRun)
            .where(QbiRun.capability == "report", QbiRun.status == "running",
                   QbiRun.chat_id.is_not(None))
            .order_by(QbiRun.created_at)
            .limit(int(_setting("report_watch_batch", 20))))
        return list(rows.scalars())


def _age_hours(run: QbiRun) -> float:
    if not run.created_at:
        return 0.0
    created = run.created_at
    if created.tzinfo is None:
        from datetime import timezone
        created = created.replace(tzinfo=timezone.utc)
    return (utcnow() - created).total_seconds() / 3600.0


async def sweep_once() -> dict:
    """扫一轮。返回 {checked, finished, gave_up, errors} 便于观测与测试。"""
    from cogni_agent.tool_center_service.quickbi import service as svc

    stats = {"checked": 0, "finished": 0, "gave_up": 0, "errors": 0}
    give_up_hours = float(_setting("report_give_up_hours", 24))
    for run in await _pending_runs():
        stats["checked"] += 1
        if _age_hours(run) > give_up_hours:
            # 超过兜底时限:给个明确结论,不要留一条永远「生成中」的记录
            await svc._update_run(
                run.id, status="failed", finished_at=utcnow(),
                error=f"报告超过 {give_up_hours:.0f} 小时仍未返回结果,已停止跟踪;"
                      f"可在 Quick BI 侧用 chatId {run.chat_id} 查看实际状态",
                error_code="REPORT_GIVE_UP")
            stats["gave_up"] += 1
            log.warning(f"报告 {run.id} 超过 {give_up_hours} 小时未完成,停止跟踪")
            continue
        # 以运行记录自身的归属构造身份:它已经是服务端的记录,不是谁的输入。
        # 显式信任这条记录的连接,避免因为发起人后来被改了景区/角色而拉不到自己的报告。
        actor = svc.Actor(user_id=run.user_id or "system:report-watcher", scenic_id=run.scenic_id,
                          role="group_admin", source="console",
                          trusted_refs=frozenset({run.connection_id, run.id}))
        try:
            latest = await svc.refresh_report(actor, run.id)
        except QuickBIError as exc:
            stats["errors"] += 1
            if exc.retryable or exc.kind == "protocol":
                # 超时 / 连不上 / 返回结构一时对不上:这次没拉到而已,报告还在远端跑。
                # 判死等于白白丢掉一份已经生成好的报告,下一轮接着拉。
                log.info(f"报告 {run.id} 这一轮没拉到({exc.kind}:{exc}),下一轮继续")
                continue
            # Quick BI 明确报错(额度用尽 / 无权限 / 配置失效)才是真失败 —— 一直重试没有意义
            await svc._update_run(run.id, status="failed", finished_at=utcnow(),
                                  error=str(exc), error_code=(exc.code or exc.kind)[:32])
            stats["finished"] += 1
            log.warning(f"报告 {run.id} 被 Quick BI 判错,停止跟踪:{exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - 未知异常一律当「这次没拉到」,下一轮重试
            stats["errors"] += 1
            log.warning(f"报告 {run.id} 轮询失败(下一轮重试):{exc}")
            continue
        if latest.get("status") != "running":
            stats["finished"] += 1
            log.info(f"报告 {run.id} 已收尾:{latest.get('status')}")
    return stats


async def run_forever(interval_seconds: float | None = None) -> None:
    """常驻循环。随应用启动,取消即退出。"""
    interval = float(interval_seconds or _setting("report_watch_interval_seconds", 30))
    log.info(f"小Q报告后台续跑已启动(每 {interval:.0f}s 扫一轮)")
    while True:
        try:
            stats = await sweep_once()
            if stats["checked"]:
                log.info(f"报告续跑:{stats}")
        except asyncio.CancelledError:
            log.info("小Q报告后台续跑已停止")
            raise
        except Exception as exc:  # noqa: BLE001 - 循环本身绝不能因为一次异常退出
            log.warning(f"报告续跑扫描异常(继续下一轮):{exc}")
        await asyncio.sleep(interval)
