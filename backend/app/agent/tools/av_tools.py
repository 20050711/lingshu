"""video_understand / audio_transcribe 工具（2026-09-15）：智能助手的媒体理解与语音转写。

- 转写：复用 services.transcriber.transcribe（本地 FunASR，与会议纪要共用一把
  全局串行锁），外加**转写缓存**（按「路径+大小+mtime」指纹落 {tools_data_dir}/av_cache，
  跨 ask 复用——FunASR 全局串行，重复转写最贵）
- 视觉：复用 services.glm_video._native_video_call（GLM 原生视频：≤20MB 且 ≤40s 直传，
  长视频按 30s 切段并发；段级文本缓存传 cache_dir 落 av_cache——不写进知识库目录）
- 产物：完整转写稿/理解结果落 ctx.output_dir（产出清单可见、read_output 可读回）；
  上下文只回填截断版（video_transcript_inject_chars / video_glm_result_chars 预算）
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from app.agent.tools import (
    ToolContext,
    ToolSpec,
    register_tool,
    resolve_output_url,
    validate_readable_path,
)
from app.core.config import get_settings
from app.core.file_utils import sanitize_filename
from app.core.logging import get_logger
from app.core.text_utils import truncate_head_tail
from app.core.url_utils import output_url
from app.services.glm_video import _native_video_call
from app.services.transcriber import _probe_duration, transcribe

logger = get_logger("agent.tools.av")
_settings = get_settings()

# 媒体扩展名（软校验：给 agent 明确纠偏提示；真正解析交给 ffmpeg/FunASR）
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".amr", ".aiff", ".oga", ".mpeg"}
_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".ts", ".m4v", ".rmvb", ".wmv", ".3gp"}


def _cache_dir() -> Path:
    """媒体工具缓存目录（转写稿 JSON + 段级视觉文本 md；随每日 03:45 清理按 30 天回收）。"""
    d = Path(_settings.tools_data_dir) / "av_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fingerprint(p: Path) -> str:
    """内容指纹：路径+大小+mtime_ns（同路径换文件不误命中；不读全文件——大视频读不起）。"""
    st = p.stat()
    return hashlib.md5(f"{p}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]


async def _emit_progress(ctx: ToolContext, tool_name: str, detail: str, brief: str) -> None:
    """工具内进度事件（前端时间线可见；对齐 video_gen 的 progress 透传格式）。"""
    if ctx.events is None:
        return
    try:
        await ctx.events.put({"event": "tool", "tool_name": tool_name, "status": "progress",
                              "subagent_id": ctx.subagent_id, "detail": detail, "brief": brief})
    except Exception:
        pass


async def _cached_transcribe(media_path: str) -> tuple[dict, bool]:
    """转写（带缓存）：返回 (结果, 是否命中缓存)。只缓存成功且有文本的结果。"""
    p = Path(media_path)
    cache_file = _cache_dir() / f"transcript_{_fingerprint(p)}.json"
    if cache_file.exists():
        try:
            data = json.loads(await asyncio.to_thread(cache_file.read_text, encoding="utf-8"))
            if data.get("text"):
                logger.info("转写命中缓存 %s", cache_file.name)
                return data, True
        except (OSError, json.JSONDecodeError):
            pass  # 缓存损坏 → 重转写
    tr = await transcribe(media_path)
    out = {"text": tr.get("text") or "", "segments": tr.get("segments") or [],
           "backend": tr.get("backend"), "source": str(p)}
    if out["text"]:
        try:
            await asyncio.to_thread(cache_file.write_text,
                                    json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.warning("转写缓存写入失败: %s", str(e)[:120])
    return out, False


def _render_transcript(tr: dict) -> str:
    """带 [mm:ss] 时间戳的分段文本（语音转写.md 同格式）；无分段回退纯文本。"""
    segs = tr.get("segments") or []
    if segs:
        return "\n".join(f"[{s.get('start', '00:00')}] {s.get('text', '')}" for s in segs)
    return tr.get("text") or ""


def _resolve_media(args_path: str, ctx: ToolContext) -> tuple[str | None, str | None]:
    """路径解析 + 越权校验：返回 (磁盘路径, 错误)。产出 URL 形式先转磁盘路径再校验。"""
    resolved = resolve_output_url(args_path, ctx) or args_path
    if not Path(resolved).exists():
        return None, (f"文件不存在：{args_path}——media_path 须为完整服务器路径"
                      "（上传文件清单 / file_search / 产出记录里的 file_path 原样复制，禁止编造）")
    denied = validate_readable_path(resolved, ctx)
    if denied:
        return None, denied
    return resolved, None


async def run_audio_transcribe(args: dict, ctx: ToolContext) -> dict:
    """音频/视频 → 语音转写（全文落盘产出；上下文回填截断版）。"""
    media_path = str(args.get("media_path") or "").strip()
    if not media_path:
        return {"error": "需要提供音频/视频文件路径（media_path）"}
    resolved, err = _resolve_media(media_path, ctx)
    if err:
        return {"error": err}
    p = Path(resolved)
    ext = p.suffix.lower()
    if ext not in (_AUDIO_EXTS | _VIDEO_EXTS):
        return {"error": f"不是音频/视频文件（{ext or '无扩展名'}）——请传媒体文件的完整服务器路径"}

    duration = await asyncio.to_thread(_probe_duration, resolved)
    if duration > _settings.media_max_duration_min * 60:
        return {"error": f"媒体时长 {duration / 60:.0f} 分钟超过上限"
                         f"（{_settings.media_max_duration_min} 分钟）——请先截取片段再转写"}

    await _emit_progress(ctx, "audio_transcribe", "正在转写（本地 FunASR，全局串行）…", "语音转写中")
    try:
        tr, cached = await _cached_transcribe(resolved)
    except Exception as e:  # noqa: BLE001
        return {"error": f"语音转写失败: {str(e)[:200]}"}
    if not tr.get("text"):
        return {"error": "转写结果为空（音频无人声或格式不支持）"}

    full = _render_transcript(tr)
    # 2026-09-15（走查）：截断后可能以点结尾（源名 "…21.08.58.01.mp4" 截成 "…21.08.58."）→
    # 拼上 ".md" 得到 "…58..md"，被下载路由当路径穿越拒（404，"下下来是错误 JSON"）。去尾部点/空格
    stem = sanitize_filename(p.stem, "media")[:40].rstrip(". ")
    fname = f"语音转写_{stem}.md"
    out_path = Path(ctx.output_dir) / fname
    out_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(out_path.write_text, full, encoding="utf-8")
    logger.info("语音转写完成 path=%s 时长=%.0fs 缓存=%s 字数=%d", resolved, duration, cached, len(full))
    return {
        "transcript": truncate_head_tail(full, _settings.video_transcript_inject_chars),
        "file_path": output_url(ctx.session_id, ctx.round_id, fname),
        "label": f"语音转写-{stem}",
        "duration_s": int(duration),
        "backend": tr.get("backend") or "",
        "note": "完整转写稿已存为产出文件（带 [mm:ss] 时间戳）；此处超长时为头尾截断版，"
                "需要更多内容用 read_output 读产出文件。",
    }


async def run_video_understand(args: dict, ctx: ToolContext) -> dict:
    """视频 → 口播转写（可关）+ GLM 画面理解；长视频由服务层按 30s 分段并逐段编号。"""
    media_path = str(args.get("media_path") or "").strip()
    if not media_path:
        return {"error": "需要提供视频文件路径（media_path）"}
    instruction = str(args.get("instruction") or "").strip() or "请描述这个视频的内容（画面、口播要点与结构）"
    need_transcript = bool(args.get("transcribe", True))

    resolved, err = _resolve_media(media_path, ctx)
    if err:
        return {"error": err}
    p = Path(resolved)
    if p.suffix.lower() not in _VIDEO_EXTS:
        return {"error": f"不是视频文件（{p.suffix or '无扩展名'}）——纯音频请用 audio_transcribe"}

    duration = await asyncio.to_thread(_probe_duration, resolved)

    transcript_full = ""
    transcript_note = ""
    if need_transcript:
        await _emit_progress(ctx, "video_understand", "正在转写口播（本地 FunASR）…", "语音转写中")
        try:
            tr, _hit = await _cached_transcribe(resolved)
            transcript_full = _render_transcript(tr)
        except Exception as e:  # noqa: BLE001
            transcript_note = f"（口播转写失败，仅画面理解：{str(e)[:120]}）"
            logger.warning("视频理解-转写失败 path=%s err=%s", resolved, str(e)[:150])

    await _emit_progress(ctx, "video_understand", "正在做画面理解（GLM 视频）…", "画面理解中")
    # 2026-09-17（用户要求：视频理解应该能选模型）：原第三参写死 None → 一直用 env 默认 GLM 视觉模型，
    # 运维在「模型配置」里改 vision 档**不生效**。现接上同一档（图片识别模型）：
    # dept/user 的 vision 配置（GLM 平台）→ 默认 glm_vision_model；非 GLM 模型在视频链路上不适用，
    # _resolve_glm_model_vision 内部已回退 GLM 默认（视频原生输入只有 GLM 支持）。
    from app.agent.tools.media_tools import _resolve_glm_model_vision

    vision_model = await _resolve_glm_model_vision(ctx)
    visual_text, verr = await _native_video_call(resolved, instruction,
                                                 {"platform": "glm", "model": vision_model},
                                                 str(_cache_dir()))
    if verr and not transcript_full:
        return {"error": f"视频理解失败: {verr}"}

    # 落盘：合并交付（画面理解 + 转写稿）+ 独立转写稿
    stem = sanitize_filename(p.stem, "video")[:40].rstrip(". ")  # 同上：去尾部点/空格
    parts = [f"# 视频理解：{p.name}\n\n", f"指令：{instruction}\n\n",
             f"## 画面理解\n\n{visual_text or f'（画面理解失败：{verr}）'}\n"]
    if transcript_full:
        parts.append(f"\n## 语音转写稿\n\n{transcript_full}\n")
    if transcript_note:
        parts.append(f"\n{transcript_note}\n")
    md = "".join(parts)
    fname = f"视频理解_{stem}.md"
    out_path = Path(ctx.output_dir) / fname
    out_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(out_path.write_text, md, encoding="utf-8")

    result: dict = {
        "analysis": truncate_head_tail(visual_text, _settings.video_glm_result_chars),
        "file_path": output_url(ctx.session_id, ctx.round_id, fname),
        "label": f"视频理解-{stem}",
        "duration_s": int(duration),
        "note": "画面理解结果与完整转写稿已存为产出文件（可下载/read_output 读回）；"
                "长视频的画面理解按「第 N 段/共 M 段」编号。",
    }
    # 2026-09-15（走查）：段成功/失败计数进 note——原模型会把"切了 N 段"汇总成"成功 N 段"
    # （实测 19 段里 12 段 1261 失败，回答仍称"成功识别19段画面"）
    import re as _re

    _seg_total = len(_re.findall(r"【第 \d+ 段/共 \d+ 段】", visual_text or ""))
    _seg_fail = len(_re.findall(r"第 \d+ 段分析失败", visual_text or ""))
    if _seg_fail:
        result["note"] += (f"**注意：画面理解共 {_seg_total + _seg_fail} 段，其中 {_seg_fail} 段失败**"
                           "——如实向用户说明，不要宣称全部成功。")
    if transcript_full:
        result["transcript"] = truncate_head_tail(transcript_full, _settings.video_transcript_inject_chars)
        tname = f"语音转写_{stem}.md"
        tpath = Path(ctx.output_dir) / tname
        await asyncio.to_thread(tpath.write_text, transcript_full, encoding="utf-8")
        result["transcript_file"] = output_url(ctx.session_id, ctx.round_id, tname)
    if transcript_note:
        result["note"] += transcript_note
    if verr:
        result["note"] += f"（画面理解部分失败：{verr}）"
    logger.info("视频理解完成 path=%s 时长=%.0fs 转写=%d字 视觉=%d字 视觉错误=%s",
                resolved, duration, len(transcript_full), len(visual_text or ""), (verr or "")[:60])
    return result


register_tool(
    ToolSpec(
        name="video_understand", progress_keys=("analysis",),
        display_name="视频理解",
        icon="video",
        summary="理解视频内容（画面理解 + 口播转写，长视频分段）",
        group="媒体",
        sort_order=12,
        select_mode="multi",
        user_description=("理解视频内容：默认先转写口播（本地 FunASR），再调 GLM 做画面理解"
                          "（分镜/场景/画面文字/产品展示）；长视频自动按 30 秒分段逐段分析，"
                          "完整结果落盘可下载。"),
        description=(
            "What：理解视频内容——默认先本地转写口播（FunASR，带时间戳），再调 GLM 视觉做画面理解；"
            "长视频自动按 30 秒分段逐段分析（画面结果带「第 N 段/共 M 段」编号）。\n"
            "When：用户要理解/分析**视频**时调用（讲了什么、分镜节奏、画面文字、产品展示、场景布置）。"
            "选型：只要文字稿或纯音频 → audio_transcribe；图片 → image_recognition。\n"
            "How：media_path 传媒体文件完整服务器路径——会话上传（【上传文件】清单）、本会话产出、"
            "或 file_search 找到的资料库视频（file_path 原样复制，**禁止编造路径**；"
            "用户说「资料库里那个视频/录音」时先 file_search 按文件名找）。"
            "instruction 写**本次一个**关注角度（如「提取画面文字」「分析分镜节奏」），"
            "多角度分多次调用、每次只问一件事；transcribe 默认 true——"
            "**首次调用后转写稿已在你上下文中**，后续内容类追问直接基于转写稿回答，不要为同一问题重复调用。\n"
            "Result：返回画面理解（analysis，长视频带分段编号）与口播转写（transcript），"
            "完整产物落盘可下载；回答引用具体发现（时间点/画面/话术），不要臆造。"
            "注意：视频超 20 分钟会因 GLM 分段上限失败——提示用户截取后重试。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "media_path": {"type": "string",
                               "description": "视频文件完整服务器路径（上传清单/file_search/产出记录里的 file_path）"},
                "instruction": {"type": "string",
                                "description": "本次理解的角度/问题（如：提取画面文字、分析分镜节奏、看产品展示）；留空做整体内容描述"},
                "transcribe": {"type": "boolean",
                               "description": "是否同时转写口播（默认 true；纯画面问题可传 false）"},
            },
            "required": ["media_path"],
        },
        queue="video",
        handler=run_video_understand,
    )
)

register_tool(
    ToolSpec(
        name="audio_transcribe", progress_keys=("transcript",),
        display_name="语音转写",
        icon="audio",
        summary="音频/视频转文字（带时间戳，全文落盘）",
        group="媒体",
        sort_order=13,
        select_mode="multi",
        user_description=("把音频或视频里的语音转成文字（本地 FunASR，带 [mm:ss] 时间戳）："
                          "支持会议录音、访谈、口播稿等；完整转写稿落盘可下载。"),
        description=(
            "What：把音频/视频里的语音转成文字（本地 FunASR 中文模型，输出带 [mm:ss] 时间戳的分段稿）。\n"
            "When：用户要**文字稿**（会议录音、访谈、口播）、或要基于录音内容总结/问答时调用；"
            "要理解**画面**内容请用 video_understand（它会顺带转写口播）；"
            "**视频只要文字稿时也用它**——不需要画面理解就别调 video_understand（省一次视觉分析）。\n"
            "How：media_path 传媒体文件完整服务器路径（会话上传的录音/视频、file_search 找到的资料库"
            "音视频——file_path 原样复制，**禁止编造路径**）。本工具**只转写不总结**——"
            "拿到转写稿后由你完成总结/提炼/问答；不要只把转写稿原样贴给用户。\n"
            "Result：返回转写稿（transcript，超长时头尾截断，完整稿落盘可用 read_output 读回）"
            "与产出文件；基于转写内容回答时引用时间点，不要臆造。"
            "注意：超过 2 小时的媒体会被拒绝——提示用户截取片段。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "media_path": {"type": "string",
                               "description": "音频或视频文件完整服务器路径（上传清单/file_search/产出记录里的 file_path）"},
            },
            "required": ["media_path"],
        },
        queue="transcribe",
        handler=run_audio_transcribe,
    )
)
