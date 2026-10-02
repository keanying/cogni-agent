"""内部心跳路由:K8s CronJob(curl 静态镜像)周期触发定时任务(2026-09-11)。

链路:K8s CronJob(每 5 分钟,集群内 Service 直连,不依赖业务镜像)
  → POST /api/internal/cron/tick(带 X-Cron-Token)
  → cronjobs/registry.tick() 判定哪些 job 到点 → 后台协程执行。

安全:共享 token(config.yaml cron.tick_token ← 环境变量 CRON_TICK_TOKEN,
K8s 注入;CronJob 与 Deployment 两侧同值)。未配置 token = 接口禁用(403),
安全默认——没有 token 就没人能触发。注意本路由挂在内层,若网关(9443)把
/api/* 全量透传,公网也能打到这个路径,token 是唯一防线,勿泄露。

运维用法:
- 常规心跳:POST /api/internal/cron/tick(不带参数,按各 job 周期判定);
- 定点触发:POST /api/internal/cron/tick?job=prewarm&force=true
  (force 忽略周期立即触发,仍受运行锁约束,可安全重复调用)。
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query

from cogni_agent.common.logging import get_logger
from cogni_agent.config import get_settings
from cogni_agent.cronjobs import registry

router = APIRouter(prefix="/internal", tags=["internal"])
log = get_logger(__name__)


def _check_token(x_cron_token: str | None) -> None:
    expected = (get_settings().get("cron", "tick_token") or "").strip()
    if not expected:
        raise HTTPException(403, "CRON_TICK_TOKEN 未配置,tick 接口禁用")
    if not x_cron_token or x_cron_token != expected:
        raise HTTPException(403, "invalid cron token")


@router.post("/cron/tick")
async def cron_tick(
    x_cron_token: str | None = Header(default=None),
    job: str | None = Query(default=None, description="只触发指定 job(缺省=全部)"),
    force: bool = Query(default=False, description="忽略周期立即触发(仍受运行锁约束)"),
):
    _check_token(x_cron_token)
    if job and job not in registry._JOBS:
        raise HTTPException(404, f"unknown cron job: {job}")
    result = await registry.tick(job_name=job, force=force)
    log.info(f"cron tick: {result}")
    return {"jobs": result}
