"""临时媒体 URL 服务（2026-08-14，D1 原生视频方案）：上传的视频经 cloudflared 隧道
以临时 URL 暴露给 GLM video_url 直拉（GLM 文件 API 不支持 mp4，官方视觉接口仅收 URL）。

安全边界：
- token 随机 32 字符（secrets.token_urlsafe(24)），不可枚举；Redis TTL 到期自动失效
- 路径白名单：仅 /data 下 uploads/tools_data 两类目录可暴露（resolve 时校验）
- 无鉴权（GLM 服务器拉取无法带 JWT）——凭 token 即临时凭证；TTL 15min（红队二次：
  原 1h 窗口偏长——token 在 URL 明文 + 公网隧道可达，缩短拉取窗口，链路为秒级拉取，15min 充裕）
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path

from app.core.config import get_settings
from app.core.redis import redis_get, redis_set

# F 扩展（红队二次）：token 格式预检正则——token_urlsafe(24) 生成 32 字符 base64url，
# 不匹配直接 404（防 junk token 打 Redis 查键）
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")


async def issue_tmp_media_url(file_path: str, ttl: int | None = None) -> str | None:
    """为本地文件签发临时公网 URL；公网入口不可用（命名隧道未配 + 临时隧道兜底失败）返回 None。

    2026-08-18：base URL 解析改为 get_public_base_url——PUBLIC_BASE_URL（命名隧道）优先，
    未配置时懒启动临时隧道兜底（只暴露 tmp-media 端点，空闲自动释放，见 services/tmp_tunnel.py）。
    """
    from app.services.tmp_tunnel import get_public_base_url

    base = await get_public_base_url()
    if not base:
        return None
    if ttl is None:
        ttl = getattr(get_settings(), "tmp_media_ttl_seconds", 900)  # 红队二次：1h → 15min（config 可调）
    token = secrets.token_urlsafe(24)
    # redis_set 无返回值（Redis 不可用时降级内存 dict，同进程可读回）；勿做真假判断
    await redis_set(f"tmp_media:{token}", str(file_path), ttl)
    # 2026-08-20（走查实锤）：GLM 视频理解按 URL 扩展名判断格式——无后缀 URL 曾返回
    # 1210"视频输入格式/解析错误"（官方示例 URL 均带 .mov/.mp4）；按文件后缀附加到 URL
    # （路由解析 {token}.mp4 形式，token 校验不变）
    suffix = Path(file_path).suffix.lower()
    url_suffix = suffix if suffix in (".mp4", ".mkv", ".mov") else ""
    # 2026-09-01（M4 硬编码修复）：前缀走 config api_prefix——与 tmp_tunnel 路由同步，随机化前缀时不 404
    api_prefix = getattr(get_settings(), "api_prefix", "/api/v1").rstrip("/")
    return f"{base}{api_prefix}/tmp-media/{token}{url_suffix}"


async def resolve_tmp_media(token: str) -> Path | None:
    """校验 token 并解析文件路径（白名单 + 存在性）。"""
    if not _TOKEN_RE.match(token):
        return None  # 格式预检（32 字符 base64url）——junk token 不打 Redis
    raw = await redis_get(f"tmp_media:{token}")
    if not raw:
        return None
    p = Path(raw)
    allowed_roots = [
        Path(get_settings().upload_dir).resolve(),
        Path(get_settings().tools_data_dir).resolve(),
    ]
    try:
        resolved = p.resolve()
    except OSError:
        return None
    # SEC-14：startswith 前缀匹配改为 is_relative_to（/data/uploads 与 /data/uploads_evil
    # 兄弟目录语义绕过面消除——同 chat.py:453 模式）
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        return None
    if not resolved.is_file():
        return None
    return resolved
