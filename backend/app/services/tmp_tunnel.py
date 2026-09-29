"""临时隧道兜底（2026-08-18 用户方案）：无命名隧道（PUBLIC_BASE_URL 未配置）时，
媒体理解需要 GLM 云端经公网 URL 拉取本地视频——懒启动"隧道专用迷你服务 + cloudflared
quick tunnel"（只暴露 tmp-media 单端点，**不挂载整个平台**），命名隧道优先、此为兜底。

延迟设计（用户要求注意延迟）：
- 懒启动 + 进程内复用：第一次需要时起，多视频/多批次共享（只付一次 3-5s 启动成本）
- 就绪等待：get_public_base_url 阻塞等 trycloudflare URL 出现（≤ tmp_tunnel_start_timeout），
  保证 GLM 拿到 URL 时隧道必已就绪（不撞启动窗口）
- 空闲回收：tmp_tunnel_idle_seconds 无 tmp_media 拉取 → 自动杀隧道+迷你服务（平时不挂公网）
- shutdown 钩子：服务停止时清理子进程

安全边界：
- 迷你服务只挂 /api/v1/tmp-media/{token} + /healthz 两路由（复用 resolve_tmp_media 的
  token 格式预检 + Redis TTL + 路径白名单），无 CORS/无 cookie/无中间件
- cloudflared 指向 127.0.0.1:{tmp_tunnel_port}——公网可见面 = tmp-media 单端点
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger("services.tmp_tunnel")
_settings = get_settings()
# 2026-09-01（M4 硬编码修复）：路由前缀走 config api_prefix——与 tmp_media 签发侧同步，随机化前缀时不 404
_api_prefix = _settings.api_prefix.rstrip("/")

_AIP_PYTHON = _settings.aip_python
_CLOUDFLARED = _settings.cloudflared_bin

# ===== 隧道专用迷你服务（独立 FastAPI，只挂 tmp-media 路由） =====
# 单独进程 + 独立端口：与主服务完全隔离，quick tunnel 只暴露这个面
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

mini_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_VIDEO_TYPES = {".mp4": "video/mp4", ".mkv": "video/x-matroska", ".mov": "video/quicktime"}


@mini_app.get("/healthz")
async def mini_health():
    return {"ok": True}


@mini_app.get(f"{_api_prefix}/tmp-media/{{token}}")
async def mini_get_tmp_media(token: str):
    from app.services.tmp_media import resolve_tmp_media

    path = await resolve_tmp_media(token)
    if path is None:
        raise HTTPException(status_code=404, detail="链接不存在或已过期")
    _mark_used()  # 拉取发生 → 刷新空闲计时（闲置自动释放的窗口从最后一次拉取起算）
    media_type = _VIDEO_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(str(path), media_type=media_type, filename=path.name)


@mini_app.get(f"{_api_prefix}/tmp-media/{{token}}.mp4")
@mini_app.get(f"{_api_prefix}/tmp-media/{{token}}.mkv")
@mini_app.get(f"{_api_prefix}/tmp-media/{{token}}.mov")
async def mini_get_tmp_media_ext(token: str):
    """2026-08-20（走查实锤）：GLM 视频理解按 URL 扩展名判断格式——带扩展名的 URL 形式
    （issue_tmp_media_url 按文件后缀签发）；token 校验与无后缀路由完全一致。"""
    return await mini_get_tmp_media(token)


# ===== 隧道生命周期状态 =====

class _TunnelState:
    lock: asyncio.Lock | None = None
    mini_proc: asyncio.subprocess.Process | None = None
    tunnel_proc: asyncio.subprocess.Process | None = None
    base_url: str | None = None          # trycloudflare URL（就绪前 None）
    last_used: float = 0.0               # 最近一次 tmp_media 拉取时间（空闲回收）
    reaper_task: asyncio.Task | None = None  # 空闲回收任务
    _drain_task: asyncio.Task | None = None  # 2026-08-20：cloudflared stdout 消费任务（防管道积压）


_state = _TunnelState()


def _ensure_lock() -> asyncio.Lock:
    if _state.lock is None:
        _state.lock = asyncio.Lock()
    return _state.lock


def _is_proc_alive(proc: asyncio.subprocess.Process | None) -> bool:
    return proc is not None and proc.returncode is None


async def _start_mini_server() -> bool:
    """启动隧道专用迷你服务（uvicorn 子进程，独立端口，仅 127.0.0.1 监听）。"""
    if _is_proc_alive(_state.mini_proc):
        return True
    try:
        proc = await asyncio.create_subprocess_exec(
            _AIP_PYTHON, "-m", "uvicorn", "app.services.tmp_tunnel:mini_app",
            "--host", "127.0.0.1", "--port", str(_settings.tmp_tunnel_port),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, FileNotFoundError) as e:
        logger.warning("隧道迷你服务启动失败: %s", str(e)[:120])
        return False
    _state.mini_proc = proc
    # 等端口就绪（轻量探测）
    import socket

    for _ in range(20):
        if proc.returncode is not None:
            logger.warning("隧道迷你服务进程退出 code=%s", proc.returncode)
            _state.mini_proc = None
            return False
        try:
            with socket.create_connection(("127.0.0.1", _settings.tmp_tunnel_port), timeout=0.3):
                return True
        except OSError:
            await asyncio.sleep(0.25)
    logger.warning("隧道迷你服务端口就绪超时")
    return False


async def _start_tunnel() -> str | None:
    """启动 cloudflared quick tunnel 指向迷你服务，解析 trycloudflare URL（就绪等待）。"""
    if _state.base_url:
        return _state.base_url
    if not Path(_CLOUDFLARED).exists():
        logger.warning("cloudflared 不存在（%s）——临时隧道不可用", _CLOUDFLARED)
        return None
    try:
        # WSL/容器环境实测（2026-08-18）：
        # - 默认 QUIC：注册成功但转发不稳（UDP receive buffer 受限、UDP Connectivity FAIL）→ curl 000
        # - http2 但默认边缘：连接注册 2s 后被边缘终止（Unregistered + Connection terminated，疑似 IPv6 双栈问题）
        # - --protocol http2 + --edge-ip-version 4：连接稳定、流量正常转发（实测 curl 200）
        proc = await asyncio.create_subprocess_exec(
            _CLOUDFLARED, "tunnel", "--url", f"http://127.0.0.1:{_settings.tmp_tunnel_port}",
            "--no-autoupdate", "--protocol", "http2", "--edge-ip-version", "4",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        logger.warning("cloudflared 启动失败: %s", str(e)[:120])
        return None
    _state.tunnel_proc = proc

    # 就绪等待：逐行读输出直到 trycloudflare URL 出现（超时 tmp_tunnel_start_timeout）。
    # 注意：wait_for 超时必须 continue（readline 无新行时继续等），不能整体跳出——
    # cloudflared 注册期间输出有 1-2s 静默窗口，过早退出会拿不到 URL（实测 bug，2026-08-18）
    url = None
    deadline = time.monotonic() + _settings.tmp_tunnel_start_timeout
    while time.monotonic() < deadline:
        if proc.returncode is not None:
            logger.warning("cloudflared 提前退出 code=%s", proc.returncode)
            break
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=2)
        except asyncio.TimeoutError:
            continue  # 注册期静默窗口：继续等待而非退出
        except asyncio.CancelledError:
            raise
        if not line:
            continue
        text = line.decode(errors="replace")
        idx = text.find("trycloudflare.com")
        if idx > 0:
            start = text.rfind("https://", 0, idx)
            if start >= 0:
                url = text[start:idx + len("trycloudflare.com")].strip()
                break

    if url:
        _state.base_url = url
        logger.info("临时隧道就绪: %s", url)
        # 2026-08-20（走查实锤）：URL 出现 ≠ 边缘转发就绪——cloudflared quick tunnel 在
        # 当前 WSL2 网络下**间歇性注册成功但转发不通**（公网拉取 530，GLM 视频 1210"输入
        # 格式/解析错误"的根因，三轮全 400 用同一个死 URL）。注册后公网验证 /healthz：
        # 不通的隧道直接释放不用（返回 None → 调用方明确报"公网入口不可用"），
        # 不再把死 URL 交给 GLM。
        _state._drain_task = asyncio.create_task(_drain_tunnel_stdout(proc))
        import httpx as _httpx

        _verified = False
        _v_deadline = time.monotonic() + min(max(_settings.tmp_tunnel_start_timeout, 10), 15)  # 验证窗口 ≤15s（不通快速放弃，由 ensure_tunnel 重试新隧道）
        while time.monotonic() < _v_deadline:
            if proc.returncode is not None:
                break
            try:
                _r = await _httpx.get(f"{url}/healthz", timeout=5)
                if _r.status_code == 200:
                    _verified = True
                    break
            except Exception:
                pass
            await asyncio.sleep(1.5)
        if not _verified:
            logger.warning("临时隧道公网验证失败（转发不通/530），释放并报不可用: %s", url)
            await _release_tunnel()
            return None
        logger.info("临时隧道公网验证通过: %s", url)
    else:
        logger.warning("临时隧道就绪超时（%ss），URL 未解析出", _settings.tmp_tunnel_start_timeout)
        await _release_tunnel()
    return _state.base_url


async def _drain_tunnel_stdout(proc: asyncio.subprocess.Process) -> None:
    """消费 cloudflared stdout 直到 EOF（防管道积压阻塞；顺带保留进程存活期间的输出可见性）。

    2026-08-20：INFO 级打印 cloudflared 输出——诊断 GLM 拉取隧道 URL 的响应状态
    （走查：GLM 拉取 1210 而公网 PowerShell 拉取 200，需看 Cloudflare 边缘侧实际响应）。
    """
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text:
                logger.info("cloudflared: %s", text[:300])
    except Exception:
        pass


async def _release_tunnel() -> None:
    """释放隧道与迷你服务（杀进程组）。"""
    for proc in (_state.tunnel_proc, _state.mini_proc):
        if _is_proc_alive(proc):
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
    if _state._drain_task:
        _state._drain_task.cancel()
        _state._drain_task = None
    _state.tunnel_proc = None
    _state.mini_proc = None
    _state.base_url = None


async def _reaper_loop() -> None:
    """空闲回收：tmp_tunnel_idle_seconds 无拉取 → 释放（平时不挂公网）。"""
    while True:
        await asyncio.sleep(60)
        if _state.base_url and _state.last_used and (
                time.monotonic() - _state.last_used > _settings.tmp_tunnel_idle_seconds):
            logger.info("临时隧道空闲 %ss 无拉取，释放", _settings.tmp_tunnel_idle_seconds)
            await _release_tunnel()


def _mark_used() -> None:
    _state.last_used = time.monotonic()


async def ensure_tunnel() -> str | None:
    """确保临时隧道就绪，返回 trycloudflare base URL；失败返回 None。

    懒启动 + 并发锁：多任务同时需要时只起一次；就绪后复用（不每任务起停——避免重复 3-5s
    启动延迟）。命名隧道配置（PUBLIC_BASE_URL）存在时本模块不接管（外层优先）。
    """
    if not _settings.tmp_tunnel_enabled:
        return None
    async with _ensure_lock():
        # 2026-08-20（走查实锤）：base_url 失效自愈——cloudflared 崩溃/被杀后旧 URL
        # 指向死隧道（公网拉取 530 → GLM 1210"视频输入格式/解析错误"，三轮全 400
        # 用同一个死 URL）。进程不在 → 清缓存重新起。
        if _state.base_url and not _is_proc_alive(_state.tunnel_proc):
            logger.warning("隧道进程已不在，清除失效 base_url 并重启隧道")
            _state.base_url = None
        if _state.base_url:
            return _state.base_url
        if not await _start_mini_server():
            return None
        # 2026-08-20：WSL2 网络下 quick tunnel 间歇性"注册成功但转发不通"（公网 530）——
        # _start_tunnel 内部公网验证失败会释放并返回 None；此处重试新隧道（最多 3 次），
        # 提高命中"能通隧道"的概率（每次尝试 ≈ 启动 3-5s + 验证 ≤15s）
        for _attempt in range(3):
            url = await _start_tunnel()
            if url:
                if _state.reaper_task is None:
                    _state.reaper_task = asyncio.create_task(_reaper_loop())
                return url
            logger.warning("隧道尝试 %d/3 验证失败，重试新隧道", _attempt + 1)
        logger.error("临时隧道 3 次尝试均未通过公网验证——公网入口不可用（视频/画面分析将失败）")
        return None


async def get_public_base_url() -> str | None:
    """tmp_media 签发用的公网 base URL：命名隧道优先，临时隧道兜底。

    - PUBLIC_BASE_URL 已配置 → 直接用（命名隧道/正式域名）
    - 未配置且 tmp_tunnel_enabled → 懒启动临时隧道（就绪等待，返回后必可用）
    - 都不行 → None（调用方按现状报"公网入口未配置"）
    """
    if _settings.public_base_url.strip():
        return _settings.public_base_url.rstrip("/")
    return await ensure_tunnel()


async def shutdown() -> None:
    """服务停止钩子（main lifespan shutdown 调用）：清理隧道与迷你服务。"""
    if _state.reaper_task:
        _state.reaper_task.cancel()
        _state.reaper_task = None
    await _release_tunnel()
