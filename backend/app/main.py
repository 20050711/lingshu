"""FastAPI 应用入口。

生命周期：启动时初始化 APScheduler（备份/重试/清理定时任务）；
关闭时释放数据库与 Redis 连接。
"""
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# 2026-09-17 下线：dashboard/upload/ceo_dashboard 三个路由不再注册
from app.api import admin, auth, chat, chunk_upload, feedback, knowledge, mcp, models, skills, tmp_media, tools_download, tools_guard, tools_meeting, tools_resume
from app.core.config import get_settings
from app.core.database import dispose_engines
from app.core.exceptions import AppError
from app.core.logging import get_logger, setup_logging
from app.core.scheduler import init_scheduler, shutdown_scheduler

setup_logging()
logger = get_logger("app.main")
_settings = get_settings()
# M4：路由前缀走配置（API_PREFIX），不再硬编码 /api/v1
_api_prefix = _settings.api_prefix.rstrip("/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 三期 M19：celery_enabled=True 时定时任务由 Celery beat 调度，API 进程不再启动 APScheduler
    if not _settings.celery_enabled:
        init_scheduler()
    # 2026-08-25（部署机预热）：启动即后台预加载 Cam++ 说话人模型（首次录音 ~20s → 预热后秒级；不阻塞启动）
    if _settings.warmup_campplus:
        import asyncio as _asyncio

        async def _warmup_campplus() -> None:
            try:
                from app.services.transcriber import _get_campplus_model

                await _asyncio.to_thread(_get_campplus_model)
                logger.info("Cam++ 说话人模型预热完成（会议录音聚类秒级就绪）")
            except Exception as e:
                logger.warning("Cam++ 预热失败（首次录音会慢 ~20s）: %s", str(e)[:100])

        _asyncio.create_task(_warmup_campplus())
    # MCP 外部工具：启动时按 mcp_tools 表动态发现并注册子工具（条目连不上只记日志）
    try:
        from app.agent.tools.mcp_dynamic import refresh_mcp_tools

        n_mcp = await refresh_mcp_tools()
        if n_mcp:
            logger.info("MCP 外部工具已就绪：%d 个子工具", n_mcp)
    except Exception:
        logger.exception("MCP 工具发现失败（不影响启动）")
    # D25（2026-08-10）：简历批次无恢复逻辑（_TASKS 进程内存）——启动时补恢复
    try:
        from app.services.resume_service import recover_stale_resume_batches

        recovered = await recover_stale_resume_batches()
        if recovered:
            logger.warning("重启恢复：%s 个中断简历批次已重置为 failed", recovered)
    except Exception:
        logger.exception("简历批次恢复失败")
    # 2026-08-25（会议纪要工具）：录音任务同为进程内存——重启后中断状态重置为 failed
    try:
        from app.services.meeting_service import recover_stale_meetings

        recovered = await recover_stale_meetings()
        if recovered:
            logger.warning("重启恢复：%s 个中断会议录音任务已重置为 failed", recovered)
    except Exception:
        logger.exception("会议录音恢复失败")
    # 会话任务后台化（2026-08-18）：agent 任务是进程内存（task_registry），重启即丢——
    # DB 中 running 会话标记 interrupted + Redis 写"上轮被中断"注记（下轮 _load_history
    # 注入引导 LLM 衔接而非重做）。用户手动重发，不自动重跑。
    try:
        from sqlalchemy import text as sa_text

        from app.core.database import get_global_engine
        from app.core.redis import redis_set

        async with get_global_engine().begin() as conn:
            rows = (
                await conn.execute(sa_text("SELECT id, last_round FROM sessions WHERE task_status='running'"))
            ).all()
            if rows:
                await conn.execute(sa_text("UPDATE sessions SET task_status='interrupted' WHERE task_status='running'"))
        for sid, lr in rows or []:
            await redis_set(f"interrupted:{sid}", str(lr or 1), 3600)
        if rows:
            logger.warning("重启恢复：%s 个运行中会话任务标记为已中断", len(rows))
    except Exception:
        logger.exception("会话任务恢复失败")
    yield
    if not _settings.celery_enabled:
        shutdown_scheduler()
    # 2026-08-18：临时隧道兜底清理（杀 cloudflared + 迷你服务子进程）
    try:
        from app.services.tmp_tunnel import shutdown as shutdown_tmp_tunnel

        await shutdown_tmp_tunnel()
    except Exception:
        logger.exception("临时隧道清理失败")
    await dispose_engines()


# M8：启动 fail-fast——JWT 签名密钥不得回落开发默认值（.env 缺失/未配置即拒绝启动）
# R1（红队三修复）：弱值/短密钥一并拒绝——仓库公开的 .env.example 模板值 change-me-in-production
# 与红队二次发现的开发默认值均在拦截列表；密钥须 >=32 字符（HS256 最低强度）
_WEAK_SECRETS = {"dev-insecure-secret-change-me", "change-me-in-production"}
if _settings.secret_key in _WEAK_SECRETS or len(_settings.secret_key) < 32:
    raise RuntimeError(
        "SECRET_KEY 未配置或为弱值（backend/.env 缺失/未设置/仍用模板值），拒绝启动——"
        "JWT 签名密钥须为 >=32 字符随机值（红队三 R1：用 scripts/rotate_secret.py 轮换）"
    )

# F21（红队二次）：关闭 /docs、/redoc、/openapi.json——红队报告 F21：全量接口 schema 泄露
#（含参数名/枚举/请求体结构，LAN 内普通用户无需登录即可浏览；无运行时消费方，仅开发调试用，
# 本地调试可在启动参数临时打开）。接口文档见 docs/交接文档/HANDOVER.md。
app = FastAPI(title="灵枢 · 智能体平台", version="0.1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

# M7：CORS 来源进配置（cors_origins 逗号分隔）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# SEC-03：CSRF 纵深——非安全方法且携带会话 cookie 的请求校验 Origin/Referer。
# 白名单 = 同源（request host）+ cors_origins 配置；无 Origin/Referer 放行
# （curl/API 客户端无 Origin；Bearer 客户端无 cookie 不受影响）。
# 浏览器层 SameSite=Lax 已挡普通跨站 POST，此为纵深（子域/浏览器降级场景）。
@app.middleware("http")
async def csrf_protect(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and "access_token" in request.cookies:
        origin = request.headers.get("origin")
        host = request.headers.get("host", "")
        if origin:
            allowed = {f"http://{host}", f"https://{host}"}
            allowed |= {o.strip() for o in _settings.cors_origins.split(",") if o.strip()}
            if origin not in allowed:
                return JSONResponse(
                    status_code=403,
                    content={"error": {"code": "E006", "message": "跨站请求被拒绝（Origin 不在白名单）"}},
                )
        else:
            referer = request.headers.get("referer")
            if referer:
                ref_host = urlparse(referer).netloc
                if ref_host and ref_host != host:
                    return JSONResponse(
                        status_code=403,
                        content={"error": {"code": "E006", "message": "跨站请求被拒绝（Referer 不在白名单）"}},
                    )
    return await call_next(request)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    body = {"code": exc.code, "message": exc.message}
    if exc.extra:
        body.update(exc.extra)  # S4：登录失败携带 captcha_id/captcha_image
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": body},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """M1：双信封统一——HTTPException 也映射为 {error:{code,message}}。

    原 admin 等 18 处 raise HTTPException(detail={"code":...}) 返回 {detail:{...}}，
    前端只能单分支解析 .error.*；统一后所有错误同信封。
    """
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": str(detail["code"]), "message": str(detail.get("message", ""))}},
        )
    # 非信封 detail（如 starlette 的 405 Method Not Allowed 字符串）→ 通用参数错误信封
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": "E011", "message": str(detail) if isinstance(detail, str) else "请求处理失败"}},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """E-15(API，2026-08-10)：未捕获异常兜底——原 DataError/ValueError/IntegrityError 等
    落到 FastAPI 默认 500 纯文本，前端单分支 .error.* 解析直接崩溃；统一映射 E016 信封。
    （具体错误码仍按各接口的 try/except 优先；此兜底只保证非信封 500 不再发生。）"""
    logger.error("未捕获异常 %s %s: %s", request.method, request.url.path, str(exc)[:300])
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "E016", "message": "服务器内部错误，请稍后重试"}},
    )


@app.get(f"{_api_prefix}/health")
async def health():
    # 2026-09-17：暴露 llm_mock——回归/测试的护栏。`MOCK=1`（e2e）只决定选哪些用例，
    # **不会**让服务端走模拟档；服务端不是模拟档时，聊天类用例会真打付费模型（当天实测
    # 一次全量跑出 49 次 deepseek 调用）。run-e2e.sh 靠这个字段提前告警。
    from app.core.config import get_settings

    return {"status": "ok", "service": "lingshu-api",
            "llm_mock": bool(get_settings().llm_mock)}


# 业务路由（前缀统一走配置 _api_prefix）
for router in (auth.router, chat.router, chat.outputs_router, skills.router, mcp.router, feedback.router, chunk_upload.router, tools_resume.router, tools_meeting.router, knowledge.router, admin.router, tools_guard.router, models.router, tmp_media.router, tools_download.router, tools_download.admin_router):
    app.include_router(router, prefix=_api_prefix)
