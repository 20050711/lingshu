"""GLM 原生视频调用（2026-09-28 抽为独立服务）。

供 av_tools 的 video_understand 使用：单段 base64 直传 / 长视频 ffmpeg 切段并发分析。
单段直传 / 长视频切段并发分析，供媒体理解工具使用。
"""
from __future__ import annotations

import asyncio
import base64
import subprocess
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.config_service import upload_limit_mb
from app.services.transcriber import _probe_duration

_settings = get_settings()
logger = get_logger("services.glm_video")

_sem_video = asyncio.Semaphore(_settings.glm_video_concurrency)  # 免费档并发很低——默认 2（可设 1 串行）

# 单段 base64 直传安全时长上限（2026-08-21 实测：40s 内全成功、44s 抖动、45s 起 1261
# Prompt 超长——GLM 视频 token 按时长/帧数计，与字节大小无关）
_GLM_VIDEO_SEG_MAX_S = 40


async def _native_video_call(video_path: str, prompt: str, vision_cfg: dict | None = None,
                             cache_dir: str | None = None) -> tuple[str, str | None]:
    """GLM 原生视频理解（不做抽帧）：video_url 直拉 / base64 直传 / 长视频切段。

    2026-08-18：vision_cfg = {platform, model}；视频理解锁定 GLM（视频原生输入）。
    2026-08-21 v2（用户决策「完全弃用临时隧道」）：
    - 单段 base64：大小 ≤glm_video_base64_max_mb 且时长 ≤40s（实测安全区）→ data URI 直传
    - 其余（长/大视频）：ffmpeg 切段 → 逐段 base64 并发调用（信号量）→ 按段拼接返回。
      段数按「时长 ÷ glm_video_split_segment_s（30s）」**和**「体积 ÷ glm_video_base64_max_mb」
      双约束取大；总时长超 glm_video_split_max_segments×段长、或段数超上限 → 报错提示截取/压缩
    - upload_mode=auto|url|base64；**url 仅手动强制保留**（auto 下不再使用隧道）
    - cache_dir：长视频段级文本缓存落该目录；None=写视频同目录

    返回 (内容, 错误)；错误非 None 时内容为空。
    """
    model = (vision_cfg or {}).get("model") or _settings.glm_vision_model

    size = Path(video_path).stat().st_size if Path(video_path).exists() else 0
    if size == 0:
        return "", "视频文件不存在"
    # 只有超过平台处理上限（video_max_size_mb）才拒；体积超单段直传上限的走切分
    if size > await upload_limit_mb("video_mb") * 1024 * 1024:
        return "", f"视频超过平台处理上限（{await upload_limit_mb('video_mb')}MB）"

    upload_mode = _settings.glm_video_upload_mode
    if upload_mode == "url":
        # 手动强制 URL 隧道（代码保留；auto 模式已完全弃用——临时隧道稳定性差）
        from app.services.tmp_media import issue_tmp_media_url

        url = await issue_tmp_media_url(video_path)
        if not url:
            return "", "公网入口不可用（未配置 PUBLIC_BASE_URL 且临时隧道不可用），暂无法视频/画面分析"
        return await _glm_video_post(_build_video_payload(url, prompt, model), size, model)

    # auto/base64：先测时长（切分与单段判定都依赖）
    duration = _probe_duration(video_path)
    if duration <= 0:
        return "", "视频时长探测失败（ffprobe 不可用），无法确定切分方案"

    if size <= _settings.glm_video_base64_max_mb * 1024 * 1024 and duration <= _GLM_VIDEO_SEG_MAX_S:
        return await _single_base64_call(video_path, prompt, model, size)

    # 长视频切分
    seg_s = _settings.glm_video_split_segment_s
    max_total_s = _settings.glm_video_split_max_segments * seg_s
    if duration > max_total_s:
        return "", (f"视频时长 {int(duration)}s 超过切分上限（{max_total_s // 60} 分钟），"
                    f"请先截取前 {max_total_s // 60} 分钟再上传")
    # 码率过高的源先转低码率代理再切——stream copy 的最小切分粒度是一个 GOP
    src, src_size = video_path, size
    proxy = await _ensure_proxy_if_needed(video_path, duration, size)
    if proxy:
        src, src_size = proxy, Path(proxy).stat().st_size
    return await _split_video_call(src, prompt, model, duration, seg_s, cache_dir, src_size)


async def _ensure_proxy_if_needed(video_path: str, duration: float, size: int) -> str | None:
    """高码率源转低码率代理（仅当必要）；返回代理路径，不需要/失败返回 None。

    为什么：切分走 ffmpeg stream copy（无损快切），**最小粒度是一个 GOP**——高码率录像
    一个 GOP 就超过 GLM 单段直传上限（实测 88Mbps/2s GOP ≈ 22MB）。代理 480p/CRF28
    按源指纹落 temp 复用；转码失败回退源视频（那些段会按失败标注，不影响其余段）。
    """
    import hashlib

    seg_s = _settings.glm_video_split_segment_s
    max_bytes = _settings.glm_video_base64_max_mb * 1024 * 1024
    if duration <= 0 or size / duration * seg_s <= max_bytes:
        return None  # 一个完整 seg_s 段都不超限 → 无需代理
    try:
        st = Path(video_path).stat()
        key = hashlib.md5(f"{video_path}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]
    except OSError:
        return None
    dst = Path(_settings.temp_dir) / f"vproxy_{key}.mp4"
    if dst.exists() and dst.stat().st_size > 0:
        return str(dst)
    # pad=ceil(iw/2)*2:ceil(ih/2)*2——force_original_aspect_ratio=decrease 会算出
    # 奇数宽（实测 853x480），libx264 直接报错（rc=187 "width not divisible by 2"）
    args = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
            "-vf", "scale=854:-2:force_original_aspect_ratio=decrease,pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(dst)]
    try:
        p = await asyncio.to_thread(subprocess.run, args, capture_output=True, text=True,
                                    timeout=max(300, min(1800, int(duration * 4))))
        if p.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            logger.warning("高码率代理转码失败（回退源视频切分）rc=%s err=%s",
                           p.returncode, (p.stderr or "")[:200])
            dst.unlink(missing_ok=True)
            return None
    except Exception as e:  # noqa: BLE001
        logger.warning("高码率代理转码异常（回退源视频切分）: %s", str(e)[:150])
        dst.unlink(missing_ok=True)
        return None
    logger.info("高码率视频已转代理：%s → %s（%.1fMB → %.1fMB）",
                Path(video_path).name, dst.name, size / 1048576, dst.stat().st_size / 1048576)
    return str(dst)


def _build_video_payload(url: str, prompt: str, model: str) -> dict:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "video_url", "video_url": {"url": url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }


async def _glm_video_post(payload: dict, size: int, model: str) -> tuple[str, str | None]:
    """单次 GLM 视频 POST（信号量限并发 + 限流/瞬时错误退避重试 1302/429/5xx → 3s/6s/12s 最多 4 次）。"""
    async with _sem_video:
        try:
            import time as _time

            _t0 = _time.monotonic()
            deadline = _time.monotonic() + _settings.video_api_retry_budget_s
            for attempt in range(4):
                try:
                    async with httpx.AsyncClient(timeout=_settings.video_api_timeout_s) as c:
                        resp = await c.post(
                            f"{_settings.zhipu_base_url}/chat/completions",
                            headers={"Authorization": f"Bearer {_settings.zhipu_api_key}"},
                            json=payload,
                        )
                except httpx.TransportError:
                    # 连接类/协议类瞬时错误统一按瞬时错误退避重试（总预算仍由 deadline 兜底）
                    if attempt == 3 or _time.monotonic() > deadline:
                        return "", "GLM 视频理解失败: 请求超时/连接异常（重试总时长超限）"
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                _cost = round(_time.monotonic() - _t0, 1)
                logger.info("GLM 视频调用耗时 %.1fs size=%dMB model=%s status=%d attempt=%d",
                            _cost, size // 1024 // 1024, model, resp.status_code, attempt)
                if resp.status_code != 200:
                    logger.warning("GLM 视频调用失败 status=%d body=%s", resp.status_code, (resp.text or "")[:200])
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"] or ""
                    return content, None
                body = (resp.text or "").lower()
                is_rate_limited = resp.status_code == 429 or "1302" in body or "速率限制" in body
                # 5xx/网关类瞬时错误同样退避重试（原只重试限流——网关 502 会直接判段失败）
                is_transient = resp.status_code >= 500
                if (is_rate_limited or is_transient) and attempt < 3 and _time.monotonic() <= deadline:
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                return "", f"GLM 视频理解失败: {resp.text[:150]}"
            return "", "GLM 视频理解失败: 重试耗尽"
        except Exception as e:  # noqa: BLE001
            return "", f"GLM 视频理解请求失败: {str(e)[:150]}"


async def _single_base64_call(video_path: str, prompt: str, model: str, size: int) -> tuple[str, str | None]:
    """单段 base64 data URI 直传（免隧道；实测 glm-4.1v-thinking-flash 接受）。"""
    import mimetypes

    mime = mimetypes.guess_type(video_path)[0] or "video/mp4"
    b64 = await asyncio.to_thread(
        lambda: base64.b64encode(Path(video_path).read_bytes()).decode("ascii"))
    url = f"data:{mime};base64,{b64}"
    logger.info("视频 base64 直传 size=%dMB model=%s", size // 1024 // 1024, model)
    return await _glm_video_post(_build_video_payload(url, prompt, model), size, model)


def _cut_video_segment(src: str, dst: str, start_s: float, dur_s: int) -> None:
    """ffmpeg stream copy 无损快切（秒级；切点对齐最近关键帧，可能偏 1 个 GOP，无碍逐段理解）。

    **`-ss` 必须在 `-i` 之前**（输入侧 seek）——输出侧 seek 配合 `-c copy` 会切出
    200~400B 的空壳文件（实测 262B、时长 N/A）→ GLM 判 1210「视频输入格式/解析错误」。
    """
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(start_s), "-i", src, "-c", "copy",
         "-t", str(dur_s), dst],
        capture_output=True, timeout=120, check=False,
    )


async def _cut_segments(video_path: str, duration: float, seg_len: float, max_bytes: int,
                        seg_dir: Path) -> list[str]:
    """按「目标段长 seg_len + 体积硬上限 max_bytes」逐段快切，返回有序分段路径。

    VBR 视频段间体积可差 2 倍——`-fs` 硬限体积 + 按**实际切出的时长**推进游标
    （截短多少、下一段就从哪儿接着切），不漏内容也不留空档。
    """
    seg_paths: list[str] = []
    pos = 0.0
    max_segs = _settings.glm_video_split_max_segments
    while pos < duration - 0.2 and len(seg_paths) < max_segs:
        dst = str(seg_dir / f"seg_{len(seg_paths):03d}.mp4")
        real = await _cut_fitted_segment(video_path, dst, pos, seg_len + 1, max_bytes)
        if real <= 0:
            break
        seg_paths.append(dst)
        pos += real
    return seg_paths


async def _cut_fitted_segment(src: str, dst: str, start: float, max_dur: float,
                              max_bytes: int) -> float:
    """切一段并**实测体积**迭代收缩到 ≤ max_bytes，返回实际时长（0=切不出来）。

    `-fs` 对 MP4 + `-c copy` **不可靠**——请求 20MB 实测切出 33MB。改为"切完量体积，
    超了就按时长比例收缩重切"（最多 3 次）；VBR 尖峰段因此被切得更短。
    """
    import math

    dur = max_dur
    real = 0.0
    for _ in range(3):
        await asyncio.to_thread(_cut_video_segment, src, dst, start, math.ceil(dur) + 1)
        try:
            real = await asyncio.to_thread(_probe_duration, dst)
            size = Path(dst).stat().st_size
        except OSError:
            return 0.0
        if real <= 0 or size == 0:
            return 0.0
        if size <= max_bytes:
            return real
        dur = max(dur * (max_bytes / size) * 0.9, 0.5)
    return real  # 三次仍超限：接受最后（最小）结果，GLM 侧失败会按段标注


async def _split_video_call(video_path: str, prompt: str, model: str, duration: float,
                            seg_s: int, cache_dir: str | None = None,
                            src_size: int | None = None) -> tuple[str, str | None]:
    """长视频切分：每 seg_s 秒一段 → 各段 base64 并发调 GLM（信号量限并发）→ 按段拼接。

    单段失败不阻断整体（该段标注失败继续）；失败段串行重试一次；全部失败返回错误。
    段级文本缓存：结果按「视频路径 + 大小 + mtime + prompt」写缓存文件；cache_dir=None
    时写视频同目录。
    """
    import hashlib
    import math
    import shutil
    import uuid

    try:
        st = Path(video_path).stat()
        fp = f"{video_path}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        fp = video_path
    cache_key = hashlib.md5(f"{fp}|{prompt}".encode()).hexdigest()[:16]
    base_dir = Path(cache_dir) if cache_dir else Path(video_path).parent
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        base_dir = Path(video_path).parent
    cache_path = base_dir / f"vseg_{cache_key}.md"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        try:
            cached = cache_path.read_text(encoding="utf-8").strip()
            if cached:
                logger.info("视频切分结果命中缓存 key=%s", cache_key)
                return cached, None
        except OSError:
            pass

    n_est = math.ceil(duration / seg_s)
    # 段数按「时长 **和** 体积」双约束取大——高码率视频一段仍可上百 MB，base64 直传必被拒
    if src_size:
        n_est = max(n_est, math.ceil(src_size / (_settings.glm_video_base64_max_mb * 1024 * 1024)))
    if n_est > _settings.glm_video_split_max_segments:
        return "", (f"视频需切约 {n_est} 段，超过上限（{_settings.glm_video_split_max_segments} 段）"
                    "——请先压缩或截取后再试")
    seg_len = duration / n_est  # 初始目标段长（≤ seg_s）
    max_bytes = _settings.glm_video_base64_max_mb * 1024 * 1024
    seg_dir = Path(_settings.temp_dir) / f"vseg_{uuid.uuid4().hex[:8]}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    try:
        seg_paths = await _cut_segments(video_path, duration, seg_len, max_bytes, seg_dir)
        n = len(seg_paths)
        if n == 0:
            return "", "视频切分失败（ffmpeg 未产出有效分段）"

        async def _one(i: int) -> str:
            seg_path = seg_paths[i]
            seg_size = Path(seg_path).stat().st_size
            content, err = await _single_base64_call(seg_path, prompt, model, seg_size)
            if err:
                return f"（第 {i + 1} 段分析失败：{err[:100]}）"
            return f"【第 {i + 1} 段/共 {n} 段】{content.strip()}"

        results = await asyncio.gather(*(_one(i) for i in range(n)))
        # 失败段**串行重试一次**（免费档并发低，偶发限流/超时/网关抖动；串行避开并发争抢）
        _failed = [i for i, r in enumerate(results) if r.startswith("（第")]
        if _failed:
            logger.info("切分分析失败 %d/%d 段，串行重试一次", len(_failed), n)
            for i in _failed:
                results[i] = await _one(i)
        parts = [r for r in results if r]
        if not parts:
            return "", "视频切分调用全部失败"
        text_out = "\n".join(parts)
        failed_n = sum(1 for r in results if r.startswith("（第"))
        if failed_n:
            text_out += f"\n（注：{failed_n}/{n} 段分析失败，其余段正常）"
        # 写缓存（部分失败也缓存——复用同样标注，避免反复重试失败段）
        try:
            await asyncio.to_thread(cache_path.write_text, text_out, encoding="utf-8")
        except OSError:
            pass
        return text_out, None
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)
