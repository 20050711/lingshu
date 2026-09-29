"""video_generate 工具（2026-08-10）：Agnes 视频生成（文生视频/图生视频/关键帧）。

- 异步任务 API：POST {agnes_video_base}/v1/videos 创建任务 → 轮询 GET /agnesapi?video_id= 等待完成
- 限流（实测）：视频 1 RPM（免费档）+ 503 video_queue_full → QueueManager 队列 video_gen 容量 1
  + 503 重试退避；每日秒数配额记账（Redis agnes_video_used:{date}），超限拒绝
- 完成：下载 metadata.url mp4 → 转存本地（防 URL 过期，对齐 image_generation 转存模式）
- 产出：output_type=video（前端 video 播放）；图生视频本地图自动 base64（官方支持，≤5MB）

官方文档：docs/官方接口规范/agens官方接口/视频模型/Agnes Video V2.0.md
"""
from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import uuid
from pathlib import Path

import httpx

from app.agent.tools import ToolContext, ToolSpec, register_tool
from app.core.config import get_settings
from app.core.url_utils import output_url

_settings = get_settings()

# duration（秒）→ num_frames（8n+1 规则，frame_rate=24）
_DURATION_FRAMES = {3: 81, 5: 121, 10: 241, 18: 441}
# ratio → 建议 width/height（agnes 会按档位标准化）
_RATIO_SIZE = {
    "16:9": (1152, 648), "9:16": (648, 1152), "1:1": (768, 768),
    "4:3": (1024, 768), "3:4": (768, 1024),
}
_HEADERS = None


def _headers() -> dict:
    global _HEADERS
    if _HEADERS is None:
        _HEADERS = {"Authorization": f"Bearer {_settings.agnes_api_key}",
                    "Content-Type": "application/json"}
    return _HEADERS


async def _url_allowed(url: str) -> bool:
    """F7（红队二次）：SSRF 校验——provider 下载 URL 与输入图 URL 共用。

    红队链 F7：下载 provider 返回的 mp4 URL 前无校验——若 Agnes 被攻破返回内网地址，
    本服务（宿主网络栈）即变 SSRF 代理；对齐 media_tools 图片下载校验。
    自站 public_base_url（tmp_media 隧道）前缀放行（平台自身地址，非攻击面）。
    """
    pub_base = (_settings.public_base_url or "").rstrip("/")
    if pub_base and url.startswith(pub_base):
        return True
    try:
        from app.core.ssrf import validate_public_url_async

        return await validate_public_url_async(url) is None
    except Exception:
        return False


async def _check_quota(duration_s: int) -> str | None:
    """每日秒数配额记账（Redis；超限返回错误信息）。"""
    try:
        from app.core.redis import redis_get, redis_set

        today = _dt.date.today().isoformat()
        key = f"agnes_video_used:{today}"
        used = int((await redis_get(key)) or 0)
        if used + duration_s > _settings.agnes_video_daily_seconds:
            return f"今日视频配额已用完（{used}/{_settings.agnes_video_daily_seconds} 秒）——请明天再试"
        await redis_set(key, str(used + duration_s), 86400 * 2)
    except Exception:
        pass  # Redis 故障不阻塞（agnes 侧仍有 RPM/配额限制）
    return None


async def _create_task(payload: dict) -> tuple[str | None, dict | None, str | None]:
    """创建视频任务（503 队列满/连接类异常均重试退避 ≤3 次）；返回 (error, task_json, video_id)。

    问题 10 修复（2026-08-17）：原实现连接超时/挂起（httpx timeout=60 无响应日志）直接返回失败
    不重试——实测 POST 挂起 57s 后失败、第二次换短 prompt 才成功；连接类异常与 503 同等重试。
    """
    last_err = ""
    async with httpx.AsyncClient(timeout=60) as c:
        for attempt in range(3):
            try:
                r = await c.post(f"{_settings.agnes_video_base}/v1/videos",
                                 headers=_headers(), json=payload)
            except Exception as e:
                # 连接类异常（ConnectTimeout/ReadTimeout 等）：重试（问题 10）
                last_err = f"Agnes 视频请求失败: {str(e)[:150]}"
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                return last_err, None, None
            if r.status_code == 200:
                j = r.json()
                return None, j, j.get("video_id")
            if r.status_code == 503:  # video queue full——退避重试
                last_err = r.text[:120]
                await asyncio.sleep(10 * (attempt + 1))
                continue
            return f"Agnes 视频创建失败 HTTP {r.status_code}: {r.text[:200]}", None, None
    return f"Agnes 视频队列持续繁忙（503）: {last_err}", None, None


async def _poll_and_download(video_id: str, ctx: ToolContext) -> tuple[str | None, str | None]:
    """轮询任务到 completed（≤agnes_video_max_wait_s），下载 mp4 转存本地。返回 (error, file_url)。"""
    out_dir = Path(ctx.output_dir)
    loop = asyncio.get_running_loop()  # 2026-08-10：get_event_loop 在 3.12 弃用
    deadline = loop.time() + _settings.agnes_video_max_wait_s
    # 2026-08-10 实测：agnès 查询端点【慢但会成功】（正常 22s，偶发 40s+）——
    # 查询超时放宽到 90s（容纳慢查询），失败仅视为网络抖动 sleep 后继续；
    # 总预算由顶部 deadline 兜底（600s），不再设固定失败次数（原 5 次导致慢查询多时误报失败）
    async with httpx.AsyncClient(timeout=90) as c:
        while True:
            # 2026-08-10：deadline 检查前置到循环顶部——异常/HTTP 失败路径 continue 前统一覆盖
            if loop.time() > deadline:
                return "视频生成超时（仍在生成中）——请稍后重新生成", None
            try:
                r = await c.get(f"{_settings.agnes_video_base}/agnesapi",
                                params={"video_id": video_id}, headers=_headers())
                if r.status_code != 200:
                    await asyncio.sleep(_settings.agnes_video_poll_interval)
                    continue
                j = r.json()
                status = j.get("status")
                # 2026-08-10：进度透传前端（tool 事件 progress 状态——前端去重键按
                # tool_name + 非终态覆盖更新，用户可见"视频生成中 30%"而非干等）
                progress = j.get("progress")
                if ctx.events is not None and progress is not None and status not in ("completed", "failed"):
                    try:
                        await ctx.events.put({"event": "tool", "tool_name": "video_generate",
                                              "status": "progress", "subagent_id": ctx.subagent_id,
                                              "detail": f"视频生成中 {progress}%（{status}）",
                                              "brief": "视频生成中"})
                    except Exception:
                        pass
                if status == "completed":
                    # 2026-08-10 实测修复（根因）：实际响应 url 在【顶层 url】字段，文档说的
                    # metadata.url 不存在（文档示例为虚构格式）——顶层优先，metadata 兜底兼容两种
                    url = j.get("url") or (j.get("metadata") or {}).get("url")
                    if not url:
                        # 兜底：带 model_name 参数重试查询（url 可能延迟就绪），3 次 × 5s 仍无则报错
                        for _attempt in range(3):
                            await asyncio.sleep(5)
                            try:
                                r2 = await c.get(
                                    f"{_settings.agnes_video_base}/agnesapi",
                                    params={"video_id": video_id, "model_name": "agnes-video-v2.0"},
                                    headers=_headers(), timeout=30)
                                j2 = r2.json()
                                url = j2.get("url") or (j2.get("metadata") or {}).get("url")
                                if url:
                                    break
                            except Exception:
                                continue
                    if not url:
                        return ("视频已完成但无下载 URL（多次查询未返回 url 字段）"
                                "——请稍后重试或联系平台", None)
                    # 下载 mp4 转存本地（防 URL 过期）
                    # F7（红队二次）：下载前 SSRF 校验（provider 返回 url 全信 → 宿主变 SSRF 代理）
                    if not await _url_allowed(url):
                        return "视频下载地址未通过安全校验（仅允许公网 URL）——请联系平台排查", None
                    try:
                        v = await c.get(url, timeout=120)
                        if v.status_code != 200:
                            return f"视频下载失败 HTTP {v.status_code}", None
                    except Exception as e:
                        return f"视频下载失败: {str(e)[:150]}", None
                    fname = f"video_{uuid.uuid4().hex[:8]}.mp4"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread((out_dir / fname).write_bytes, v.content)
                    return None, output_url(ctx.session_id, ctx.round_id, fname)
                if status == "failed":
                    return f"视频生成失败: {str(j.get('error') or '未知错误')[:200]}", None
            except Exception:
                # 网络抖动容错：查询异常仅 sleep 后继续（deadline 顶部检查兜底总预算）
                await asyncio.sleep(_settings.agnes_video_poll_interval)
                continue
            await asyncio.sleep(_settings.agnes_video_poll_interval)


async def run_video_generate(args: dict, ctx: ToolContext) -> dict:
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        return {"error": "需要提供视频描述 prompt"}
    mode = str(args.get("mode") or "text2video")
    duration = int(args.get("duration") or 5)
    if duration not in _DURATION_FRAMES:
        duration = 5
    num_frames = _DURATION_FRAMES[duration]
    frame_rate = 24
    ratio = str(args.get("ratio") or "16:9")
    w, h = _RATIO_SIZE.get(ratio, (1152, 768))

    # 2026-09-17（缓存审计 P2-12）：同参数 10 分钟内重复触发 → 复用上次视频（视频还有每日秒数配额，
    # 重复一次就是真烧配额）；确实要重新生成传 regenerate=true。放在配额检查**之前**（复用不耗配额）
    from app.services import gen_dedupe

    _sig = {"prompt": prompt, "mode": mode, "duration": duration, "ratio": ratio,
            "keyframes": args.get("keyframes") or args.get("image_path") or ""}
    if not bool(args.get("regenerate")):
        prev = await gen_dedupe.recent("video", ctx.user_id or 0, _sig)
        if prev and prev.get("file_path"):
            return {**{k: v for k, v in prev.items() if not k.startswith("_")},
                    "note": f"（同一参数 {prev.get('_ago_min', 0)} 分钟前刚生成过，直接复用上次的视频；"
                            "确实要重新生成请把 regenerate 设为 true）"}

    # 每日配额记账
    quota_err = await _check_quota(duration)
    if quota_err:
        return {"error": quota_err}

    payload: dict = {
        # 2026-08-14：用户级覆盖优先（技能卡视频生成档；catalog 仅 agnes-video 一族）
        "model": (ctx.aux_overrides or {}).get("video_generate", {}).get("model") or "agnes-video-v2.0",
        "prompt": prompt,
        "width": w,
        "height": h,
        "num_frames": num_frames,
        "frame_rate": frame_rate,
    }
    # 2026-08-10 实测修复：mode 仅 keyframes 合法——图生视频/文生视频必须省略 mode（传 image2video 会 400）
    if mode == "keyframes":
        payload["mode"] = "keyframes"
    if args.get("negative_prompt"):
        payload["negative_prompt"] = str(args["negative_prompt"])
    if args.get("seed") is not None:
        payload["seed"] = int(args["seed"])

    # 图片来源（2026-08-10 实测）：公网 URL 需能被 Agnes 服务端直接下载（防盗链站点会 400
    # "image URL could not be downloaded"）——**本地图 base64 最可靠**；本地路径支持 work 目录相对路径
    image = str(args.get("image") or "").strip()
    extra_images = args.get("extra_images") or []
    if image:
        if image.startswith(("http://", "https://")):
            # F7：输入图 URL 过 SSRF 校验（该 URL 由 Agnes 服务端拉取、我方不直连，
            # 防内网 URL 被第三方探测；非法时提示改用 base64 上传——本地图最可靠）
            if not await _url_allowed(image):
                return {"error": "图片 URL 未通过安全校验（仅允许公网 HTTP(S) 地址）——"
                                 f"可改用本地文件路径或 base64 上传（公网 URL 亦需能被视频服务直接下载）"}
            payload["image"] = image
        else:
            b64 = await _local_image_base64(image, ctx)
            if b64 is None:
                return {"error": f"本地图片读取失败或超过 {_settings.agnes_image_max_bytes // 1024 // 1024}MB 上限：{image}"
                                f"（提示：可先用 run_script 把图片下载/处理到 work 目录，再传文件名；"
                                f"公网 URL 需能直接被视频服务下载，防盗链站点会失败）"}
            payload["image"] = b64
    if extra_images:
        keyframes: list[str] = []
        for p in extra_images:
            s = str(p).strip()
            if s.startswith(("http://", "https://")):
                if not await _url_allowed(s):
                    return {"error": f"关键帧图片 URL 未通过安全校验（仅允许公网地址）：{s[:60]}"
                                     f"——可改用本地文件路径/base64 上传"}
                keyframes.append(s)
            else:
                b64 = await _local_image_base64(s, ctx)
                if b64 is None:
                    return {"error": f"关键帧图片读取失败或超限：{s}"}
                keyframes.append(b64)
        if keyframes:
            payload["extra_body"] = {"image": keyframes, "mode": "keyframes"}

    # 创建任务（503 重试）→ 轮询完成 → 下载转存
    err, task, video_id = await _create_task(payload)
    if err:
        return {"error": err}
    err2, file_url = await _poll_and_download(video_id, ctx)
    if err2:
        return {"error": err2}
    fname = file_url.rsplit("/", 1)[-1]
    label = f"视频-{prompt[:20]}.mp4"
    result = {
        "file_path": output_url(ctx.session_id, ctx.round_id, fname),
        "output_type": "video",   # tool_exec 产出收集按此分型（前端 video 播放）
        "label": label,
        "duration_s": duration,
        "note": f"视频已生成（{duration} 秒，{mode}）并转存本地，前端可播放/下载。",
    }
    await gen_dedupe.remember("video", ctx.user_id or 0, _sig,
                              {k: v for k, v in result.items() if k != "note"})
    return result


async def _local_image_base64(path: str, ctx: ToolContext) -> str | None:
    """本地图片 → data URI（agnes 官方支持 base64 image data；≤5MB）。

    2026-08-10 修复：支持 work 目录相对路径（run_script 的 cwd=work 目录，LLM 视角的
    文件名如 liuying_video.png 应解析到 {sandbox_dir}/{session}/{round}/work/ 下）；
    绝对路径/相对 cwd 顺序尝试。
    """
    candidates = [Path(path)]
    if not path.startswith("/"):
        candidates.insert(0, Path(_settings.sandbox_dir) / str(ctx.session_id) / str(ctx.round_id) / "work" / path)
    for p in candidates:
        try:
            if not p.exists() or p.stat().st_size > _settings.agnes_image_max_bytes:
                continue
            data = await asyncio.to_thread(p.read_bytes)
            suffix = p.suffix.lower().lstrip(".") or "png"
            if suffix in ("jpg", "jpeg"):
                suffix = "jpeg"
            return f"data:image/{suffix};base64,{base64.b64encode(data).decode()}"
        except Exception:
            continue
    return None


register_tool(
    ToolSpec(
        name="video_generate", progress_keys=("file_path",),
        write=True,
        queue="video_gen",
        display_name="视频生成",
        icon="film",
        summary="生成短视频（文生视频/图生视频/关键帧动画，Agnes 异步任务）",
        group="产出",
        sort_order=9,
        user_description=(
            "生成短视频（3-18 秒）：输入文字描述即可生成；支持图片转视频（本地图片自动编码上传）"
            "与关键帧动画。生成需要 1-3 分钟，完成后可播放/下载。"
            "（同一参数 10 分钟内重复请求会直接复用上次视频；用户明确要「再来一版」时传 regenerate=true）"
        ),
        description=(
            "What：生成短视频（Agnes 异步任务 API）——文生视频（prompt）/ 图生视频（image）/ "
            "关键帧动画（extra_images 数组 2+ 张）。\n"
            "When：用户要求生成视频/宣传短片/产品演示/图片动画时调用。\n"
            "How：prompt 描述主体+动作+场景+镜头+光线+风格；**mode 仅关键帧动画传 keyframes，"
            "文生视频/图生视频不要传 mode 参数**（传 image2video 会被拒绝 400）；"
            "duration=3/5/10/18 秒（默认 5）；ratio=16:9|9:16|1:1|4:3|3:4（默认 16:9）；"
            "image 传本地文件路径（**自动 base64 上传最可靠，支持 work 目录文件名或绝对路径**，≤5MB）"
            "或公网图片 URL（需能被视频服务直接下载，防盗链站点会失败）；extra_images 传关键帧数组；"
            "negative_prompt 排除不希望的内容；seed 固定可复现。\n"
            "Result：生成需要 1-3 分钟（工具内等待，完成后返回视频文件）；前端可播放/下载。\n"
            "**边界：等待属正常，别对同一任务重复触发；失败（内容审核/服务不可用）→ 改描述重试一次或"
            "如实告知用户，不要连续硬试。**"
            "视频模型有限流（每分钟 1 次）与每日时长配额，失败会返回错误说明。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "视频内容描述（主体+动作+场景+镜头+光线+风格）"},
                "mode": {"type": "string", "enum": ["text2video", "keyframes"],
                         "description": "仅关键帧动画传 keyframes；文生视频/图生视频不传此参数"},
                "image": {"type": "string", "description": "图生视频输入：本地文件路径（自动 base64，最可靠）或公网图片 URL"},
                "extra_images": {"type": "array", "items": {"type": "string"},
                                 "description": "关键帧模式：2+ 张图片（路径或公网 URL）"},
                "duration": {"type": "integer", "enum": [3, 5, 10, 18], "description": "视频时长（秒），默认 5"},
                "ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1", "4:3", "3:4"],
                          "description": "宽高比，默认 16:9"},
                "negative_prompt": {"type": "string", "description": "反向提示词（排除不希望的内容）"},
                "seed": {"type": "integer", "description": "随机种子（固定可复现）"},
                "regenerate": {"type": "boolean",
                               "description": "确实要重新生成（默认 false：10 分钟内同参数直接复用上次结果，不耗配额）"},
            },
            "required": ["prompt"],
        },
        handler=run_video_generate,
    )
)
