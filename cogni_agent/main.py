"""
智旅 zhilv-agent-core · 应用入口(一键启动)。

`uvicorn app.main:app` 或 `python -m app.main` 即可同时拉起:
  - FastAPI HTTP/SSE 服务(gateway_service 挂载的全部路由)
  - worker/ 后台协程(任务认领循环 · Outbox 派发 · 租约巡检)
这就是"模块化单体一键启动"的完整体现——不需要分别部署多个服务。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from cogni_agent.common.errors import ZhilvError
from cogni_agent.common.logging import configure_logging, get_logger
from cogni_agent.common.middleware import ApiResponseMiddleware, TraceIdMiddleware
from cogni_agent.config import get_settings
from cogni_agent.gateway_service.middleware import RateLimitMiddleware
from cogni_agent.gateway_service.routes_admin import router as admin_router
from cogni_agent.gateway_service.routes_api_keys import router as api_keys_router
from cogni_agent.gateway_service.routes_app import router as app_router
from cogni_agent.gateway_service.routes_feature_docs import router as feature_docs_router
from cogni_agent.gateway_service.routes_internal import router as internal_router
from cogni_agent.gateway_service.routes_chat import router as chat_router
from cogni_agent.gateway_service.routes_openai import router as openai_router
from cogni_agent.gateway_service.routes_tasks import router as tasks_router
from cogni_agent.gateway_service.routes_graph import router as graph_router
from cogni_agent.gateway_service.routes_app_ops import router as app_ops_router
from cogni_agent.gateway_service.routes_params import router as params_router
from cogni_agent.governance_event_bus_service.audit import audit_subscriber
from cogni_agent.governance_event_bus_service.cost import cost_subscriber
from cogni_agent.governance_event_bus_service.events.dispatcher import register_subscriber
from cogni_agent.governance_event_bus_service.routes import router as governance_router
from cogni_agent.knowledge_service.routes import router as knowledge_router
from cogni_agent.runtime_service.engine.checkpoint import close_checkpointer, get_checkpointer
from cogni_agent.tool_center_service.routes import router as tool_center_router
from cogni_agent.tool_center_service.quickbi.routes_admin import router as quickbi_admin_router
from cogni_agent.tool_center_service.quickbi.routes_open import router as quickbi_open_router
from cogni_agent.user_center_service.routes import router as user_center_router
from cogni_agent.worker.loops import start_background_loops

log = get_logger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    register_subscriber(audit_subscriber)  # M7-04
    register_subscriber(cost_subscriber)   # M7-05
    # 启动前自动同步数据库结构(全新库建基线 + 增量迁移 + 结构体检,全部幂等)。
    #
    # 这里**故意不吞异常**。之前的版本把它包在 try/except 里只记一行日志,结果是:
    # 导入失败 -> 建表被跳过 -> 服务照常起来 -> 库里只有 checkpointer 自建的 4 张表,
    # 而使用者完全不知道发生了什么,只会在后续操作里撞上各种「表不存在」。
    # 结构没同步成功,这个服务本来就不可用,不如在启动阶段就明确失败。
    #
    # 注意:老库(已有表)不再自动补列/建缺表——「改了模型没写迁移」时 sync_schema
    # 会在结构体检直接 raise,在这里拒绝启动(有迁移记录才放行,见 schema.py)。
    # 确实需要跳过(如结构由 DBA 统一管理)时,配置 database.auto_sync_schema: false。
    if get_settings().get("database", "auto_sync_schema", default=True):
        from cogni_agent.common.schema import sync_schema

        stats = await sync_schema(verbose=False)
        log.info(
            f"数据库结构已同步:共 {stats['total_tables']} 张表"
            + (f",全新库建基线 {len(stats['created_tables'])} 张" if stats["created_tables"] else "")
            + (f",应用迁移 {stats['applied_migrations']}" if stats["applied_migrations"] else "")
            + ",结构体检通过(模型与库一致)"
        )
        await _first_run_seed()

    _log_login_policy()

    await get_checkpointer()  # 提前建好 LangGraph Checkpoint 存储(建表/连接池),避免首个请求现建
    await _redis_preflight()
    await _sandbox_preflight()
    from cogni_agent.governance_event_bus_service.policy import ensure_seeded
    await ensure_seeded()  # 策略表为空 = 所有判定都拒绝,必须在启动阶段兜住
    # 数据指标接口文档里的 30 个 HTTP 工具:缺的自动建上(已有的不动,失败只记日志)
    from cogni_agent.tool_center_service.data_api_seed import ensure_on_startup as _ensure_data_api_tools
    await _ensure_data_api_tools()
    log.info("starting background worker loops (claim / dispatcher / lease-patrol)")
    tasks = start_background_loops()

    # 小Q报告要跑几分钟到几十分钟,发起它的那条 HTTP 连接活不了那么久。
    # 收结果这件事必须由服务端自己扛着,否则页面一刷新就再也没人去收,
    # 运行记录永远停在「生成中」。放在这里而不是只依赖 /cron/tick ——
    # 那个心跳由 K8s CronJob 驱动,单机部署未必配了。
    if get_settings().get("quickbi", "report_watch_enabled", default=True):
        import asyncio as _asyncio

        from cogni_agent.tool_center_service.quickbi.report_watcher import run_forever as _watch_reports

        tasks = list(tasks) + [_asyncio.create_task(_watch_reports())]

    yield
    for t in tasks:
        t.cancel()
    await close_checkpointer()
    log.info("zhilv-agent-core shutdown complete")


async def _redis_preflight() -> None:
    """启动时确认 Redis 可用。

    **为什么必须单独检查**:Redis 不通时服务照样能启动,直到第一个请求打进来才
    以 500 的形式暴露 —— 而日志里只有一行 `GET /health 500`,完全看不出是 Redis 的问题。
    运维看到「服务起来了但一访问就 500」,排查方向大概率会跑偏到应用代码上。

    **不阻断启动**:Redis 只承载 SSE 事件流与限流,数据库才是真正的事实源。
    Redis 短暂不可用时降级运行(内存流)比整个服务拒绝启动好。但必须**大声告警**。
    """
    from cogni_agent.config import get_settings

    cfg = get_settings().get("redis", default={}) or {}
    host, port = cfg.get("host", "?"), cfg.get("port", "?")
    try:
        import redis.asyncio as aioredis

        client = aioredis.Redis(
            host=host, port=port, password=cfg.get("password") or None,
            db=cfg.get("db", 0), socket_connect_timeout=5, socket_timeout=5,
        )
        try:
            await client.ping()
            log.info(f"Redis 连接正常({host}:{port})")
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001
        log.error(
            f"**Redis 不可用**({host}:{port}):{exc}\n"
            f"  影响:SSE 流式输出会退化为单进程内存模式(多实例部署下,"
            f"客户端连到哪个实例就只能收到哪个实例的事件),限流也会失效。\n"
            f"  处理:启动 Redis(`docker compose up -d redis` / "
            f"`sudo systemctl start redis`),或改 .env 里的 REDIS_HOST/REDIS_PORT。\n"
            f"  服务会继续启动 —— Redis 不是事实源,数据库才是。"
        )


async def _sandbox_preflight() -> None:
    """沙箱启动自检。

    - docker 模式:确认 docker 守护进程可用、镜像已拉取。不可用时**大声报错但不阻止
      启动**——只有 Python 类型的工具会受影响,让整个平台起不来反而是更大的故障。
    - local 模式:如果没配专用运行用户,提示一次风险(RLIMIT_NPROC 是按用户计的,
      沙箱与主服务同用户时拦不住 fork 炸弹)。
    """
    from cogni_agent.config import get_settings

    sb = get_settings().get("tool_center", "sandbox", default={}) or {}
    mode = sb.get("mode", "local")
    if mode == "docker":
        from cogni_agent.tool_center_service.sandbox.docker_exec import preflight

        ok, detail = await preflight()
        if ok:
            log.info(f"沙箱模式 docker:{detail}")
        else:
            log.error(f"沙箱模式配置为 docker,但自检未通过:{detail}。"
                      f"Python 插件类工具将无法执行(http/db 类型不受影响)")
    else:
        if not sb.get("python_run_as_user"):
            log.warning(
                "沙箱模式 local 且未配置 python_run_as_user:Python 插件与主服务以同一用户运行,"
                "RLIMIT_NPROC 无法真正隔离(fork 炸弹会拖垮主服务)。"
                "生产环境请配置专用低权限用户,或切到 docker 模式。详见 README「沙箱安全」"
            )


async def _first_run_seed() -> None:
    """首次启动(用户表为空)时自动灌种子数据。

    没有这一步的话,全新部署起来之后是一个「表都在、但一个用户都没有」的状态,
    登录只会返回 401「账号/手机号或密码错误」——而使用者会以为是密码记错了,
    很难联想到"库里压根没有账号"。
    只在**完全为空**时执行,不会覆盖任何已有数据。
    """
    from sqlalchemy import text

    from cogni_agent.common.db import get_engine

    async with get_engine().begin() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM numeri_core_usr_users"))).scalar()
    if n:
        return

    log.warning("检测到用户表为空(首次部署),正在自动创建超级管理员与权限策略 …")
    try:
        from cogni_agent.governance_event_bus_service.policy import seed_policies
        from scripts.seed_superadmin import main as seed_admin
    except ModuleNotFoundError:
        # 以安装包方式部署时 scripts/ 可能不在,退回只灌策略并提示手动执行
        from cogni_agent.governance_event_bus_service.policy import seed_policies

        seed_policies()
        log.warning("已灌入权限策略。请手动执行 python scripts/init_db.py --with-seed 创建管理员账号")
        return

    seed_policies()
    await seed_admin()


def _log_login_policy() -> None:
    """把登录策略明确打到启动日志里。

    「要不要验证码」是登录失败时最容易搞不清的一件事,启动时打印一行,
    排查时就不用去翻配置文件对照了。
    """
    cfg = get_settings().get("user_center", default={})
    require_sms = bool(cfg.get("require_sms_on_first_login", False))
    provider = (cfg.get("sms") or {}).get("provider", "mock")
    if require_sms:
        log.warning(
            f"登录策略:**首次登录需要短信验证码**(require_sms_on_first_login=true,"
            f"provider={provider})。短信通道若仍是 mock,验证码只会打印在本日志里。"
            f"想关掉:config.yaml -> user_center.require_sms_on_first_login: false"
        )
    else:
        log.info("登录策略:账号/手机号 + 密码即可登录(未开启短信验证码)")


app = FastAPI(title="智旅 zhilv-agent-core", version="1.0.0", lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(RateLimitMiddleware)          # M1-03
app.add_middleware(TraceIdMiddleware)            # G-LOG-04
app.add_middleware(ApiResponseMiddleware)        # 统一 ApiResponse 包裹 —— 最内层，最先拿到 handler 原始返回


@app.exception_handler(ZhilvError)
async def zhilv_error_handler(request, exc: ZhilvError):
    from cogni_agent.common.logging import get_trace_id
    from cogni_agent.common.schemas import ApiResponse, ErrorCode
    from fastapi.responses import JSONResponse

    _CODE_MAP = {
        "unauthorized": ErrorCode.UNAUTHORIZED,
        "forbidden": ErrorCode.FORBIDDEN,
        "validation_error": ErrorCode.INVALID_PARAM,
        "not_found": ErrorCode.NOT_FOUND,
        "rate_limited": ErrorCode.RATE_LIMITED,
        "internal_error": ErrorCode.INTERNAL_ERROR,
        "guardrail_tripped": ErrorCode.GUARDRAIL_TRIPPED,
        "approval_rejected": ErrorCode.INTERNAL_ERROR,
        "resolve_failed": ErrorCode.INVALID_PARAM,
        "api_key_invalid": ErrorCode.API_KEY_INVALID,
        "agent_not_allowed": ErrorCode.AGENT_NOT_ALLOWED,
    }
    # 若异常实例携带了自定义 error_code,优先使用；否则走类级别 CODE_MAP 映射
    numeric_code = exc.error_code if exc.error_code is not None else _CODE_MAP.get(exc.code, ErrorCode.UNKNOWN)
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.fail(code=numeric_code, message=str(exc), trace_id=get_trace_id()).model_dump(),
    )


# ── 路由挂载:统一在这里加 /api 前缀,而不是让每个 router 各自决定要不要带 ──
#
# 之前每个 router 内部自己定义前缀(如 "/chat"、"/admin"),这里 include_router() 时
# 不带 /api,而前端 lib/api.ts 的 BASE 默认是 "/api"。开发环境靠 vite.config.ts 的
# 代理把 /api 剥掉再转发,天衣无缝;但生产环境是本文件直接 `StaticFiles` 托管
# web/dist(见下方),没有任何代理层做剥前缀这件事——于是前端发出的 POST /api/auth/login
# 匹配不到任何一个 API 路由,直接落到 StaticFiles 的兜底挂载点上,而 StaticFiles
# 只认 GET/HEAD,对 POST 一律回 405 Method Not Allowed。这不是"个别接口"的问题,是
# *所有* API 请求在生产模式下都会 404/405,开发模式因为有代理掩盖了这个不一致,
# 一直没暴露出来。统一在 include_router 时补上 prefix="/api",开发/生产两种场景
# 才会走到同一套真实路径,vite.config.ts 的代理也相应改成不剥前缀直接转发。
app.include_router(chat_router, prefix="/api")      # M1 /api/admin/chat
app.include_router(openai_router)                   # G-API 开放平台协议——刻意不加 /api 前缀,
                                                      # 保持 /v1/chat/completions 这个路径,匹配
                                                      # OpenAI SDK 的默认 base_url 约定(外部调用方
                                                      # 会直接把 base_url 设成 https://host/v1,不会
                                                      # 平白多拼一段 /api);这条路由本来就不是给内部
                                                      # 管理后台前端用的,不受上面那条 /api 前缀问题影响。
app.include_router(tasks_router, prefix="/api")     # M3 /api/admin/tasks
app.include_router(admin_router, prefix="/api")     # 管理后台聚合
app.include_router(tool_center_router, prefix="/api")   # 工具中心 /api/admin/tools,/api/admin/skills,/api/admin/agents,/api/admin/mcp-servers
app.include_router(quickbi_admin_router, prefix="/api")  # 工具中心 · Quick BI 智能小Q /api/admin/quickbi/*
app.include_router(quickbi_open_router)                 # 开放平台 · Quick BI /v1/quickbi/*(API Key,与 /v1/chat 同级不加 /api)
app.include_router(knowledge_router, prefix="/api")      # 知识中心 /api/admin/knowledge/*
app.include_router(governance_router, prefix="/api")     # 审批中心/工作台看板/系统设置 /api/admin/approvals,/api/admin/dashboard/*,/api/admin/settings/*
app.include_router(user_center_router, prefix="/api")   # M2 /api/admin/auth, /api/admin/users
app.include_router(api_keys_router, prefix="/api")    # /api/admin/api-keys
app.include_router(graph_router, prefix="/api")       # 链路可视化 /api/admin/graph/*
app.include_router(app_ops_router, prefix="/api")     # APP 运营 /api/admin/app-ops/*(19号:APP端业务后台)
app.include_router(params_router, prefix="/api")     # 系统参数 /api/admin/params/*(0090:系统设置 · 参数配置,Tool/MCP/Skill 用 {{参数}} 引用)
app.include_router(feature_docs_router, prefix="/api")  # 系统功能说明 /api/admin/feature-docs(管理端文档,只读)
# 内部心跳 /api/internal/cron/tick(K8s CronJob curl 触发定时任务,共享 token 鉴权,
# 见 cogni_agent/cronjobs/registry.py——K8s 侧一次性配置,调度逻辑随发版演进)
app.include_router(internal_router, prefix="/api")
# APP 端(/api/app/*)。router 内部已带 prefix="/api/app",这里不再叠加 —— 它与管理端
# 共用同一套认证与用户逻辑,只是路由前缀和响应包裹形状不同。
app.include_router(app_router)


@app.get("/health")
async def health():
    """健康检查。版本号取自已安装包的元数据,不写死——登录页底部会展示它,
    写死的话每次发版都得记得手改,迟早对不上。"""
    try:
        from importlib.metadata import version

        ver = version("cogni-agent")
    except Exception:  # noqa: BLE001 - 源码方式运行(未安装)时取不到
        ver = "dev"
    # **逐项体检而不是无脑返回 ok。** 健康检查的意义在于告诉运维「哪里坏了」,
    # 不看依赖只回 ok 的话,负载均衡会一直把流量打到一个数据库已经断连的实例上。
    checks: dict[str, str] = {}
    degraded = False

    try:
        from cogni_agent.common.db import get_engine

        async with get_engine().connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {str(exc)[:120]}"
        degraded = True

    try:
        from cogni_agent.runtime_service import streaming

        if streaming._redis_enabled():
            # **不能复用 streaming 那个客户端**:它的 socket 超时是按长轮询调的
            # (block + 10 秒),健康探针拿它去 ping,Redis 挂掉时会**挂住 25 秒**
            # 而不是快速报错 —— 负载均衡看到的是「这个实例很慢」,不是「它有问题」,
            # 于是继续往上打流量直到自己超时。探针必须快速失败。
            import redis.asyncio as aioredis

            from cogni_agent.config import get_settings

            rcfg = get_settings().get("redis", default={}) or {}
            probe = aioredis.Redis(
                host=rcfg.get("host"), port=rcfg.get("port"),
                password=rcfg.get("password") or None, db=rcfg.get("db", 0),
                socket_connect_timeout=2, socket_timeout=2,
            )
            try:
                await probe.ping()
                checks["redis"] = "ok"
            finally:
                await probe.aclose()
        else:
            checks["redis"] = "disabled"
    except Exception as exc:  # noqa: BLE001
        # Redis 挂了是**降级**不是**故障**:SSE 退回内存模式仍能服务单实例请求。
        # 所以标 degraded 而不是 error —— 让运维知道要修,但别把实例摘掉。
        checks["redis"] = f"degraded: {str(exc)[:120]}"

    # 数据库不通才算 error(那时什么都干不了);Redis 不通只是能力受限
    status = "error" if degraded else "ok"
    body = {"status": status, "service": "zhilv-agent-core", "version": ver, "checks": checks}
    if degraded:
        # 返回 503 让负载均衡/K8s 探针能摘掉这个实例 —— 只看 body 不看状态码的探针很常见
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content=body)
    return body



# ── 开发者 API 文档(免登录) ────────────────────────────────────────
# 对外集成方拿不到后台账号,却正需要这份文档 —— 所以它必须是公开的静态页,
# 不能藏在登录后的菜单里。内容只描述对外接口,不含任何密钥或内部地址。
# 注册在 web/dist 的兜底挂载**之前**,否则会被前端的 index.html 接管。
_DEV_DOC = Path(__file__).resolve().parent / "static" / "developer" / "index.html"


@app.get("/developer", include_in_schema=False)
@app.get("/developer/", include_in_schema=False)
async def developer_docs():
    """开发者 API 文档。无需鉴权,浏览器直接打开 /developer 即可。"""
    from fastapi.responses import FileResponse, JSONResponse

    if not _DEV_DOC.exists():
        return JSONResponse(status_code=404, content={"code": 404, "message": "开发者文档未随包发布", "data": None})
    return FileResponse(str(_DEV_DOC), media_type="text/html; charset=utf-8")


# ── 生产环境可选:托管前端构建产物(web/dist),实现"一个包"访问管理后台 ──
_web_dist = Path(__file__).resolve().parent.parent / "web" / "dist"
if _web_dist.exists():
    app.mount("/", StaticFiles(directory=str(_web_dist), html=True), name="web")
