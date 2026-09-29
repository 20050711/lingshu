"""会议纪要工具后台任务（2026-08-25 定制化工具）。

状态机：uploaded → transcribing → ready → summarizing → done / failed
- uploaded：前端上传录音文件落盘 + 插行
- transcribing：后台转写（复用 transcriber.transcribe——共用 FunASR 与 _funasr_lock 串行）
- ready：转写完成，等用户选场景点"生成总结"（需求：用户点击开始总结）
- summarizing：LLM 总结中（场景提示词 / 自定义提示词）
- done：总结落盘，可下载 zip（原始录音 + 语音转写.md + 总结.md）

目录：{tools_data_dir}/meeting/{meeting_id}/（录音.{ext} / 语音转写.md / 总结.md）
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_global_engine
from app.core.logging import get_logger
from app.core.text_utils import sanitize_db_text, truncate_head_tail
from app.agent.prompts.meeting_tasks import CUSTOM_SUMMARY_SYSTEM, MEETING_SCENES
from app.services.transcriber import _probe_duration, transcribe

logger = get_logger("services.meeting")
_settings = get_settings()

_MEETING_TASKS: dict[str, asyncio.Task] = {}  # meeting_id -> 后台任务（重启即丢，进度以 DB 为准）

# 2026-08-27（整体超时兜底）：FunASR 转写挂起会持 _funasr_lock 卡全局转写——
# 超时取消任务（async with 锁 finally 自动释放），记录置 failed 可重新转写
_TRANSCRIBE_TIMEOUT_S = 1800


def _meeting_dir(meeting_id: str) -> Path:
    return Path(f"{_settings.tools_data_dir}/meeting/{meeting_id}")


def _launch_meeting(meeting_id: str) -> None:
    """后台启动转写（fire-and-forget；三期 M19：celery_enabled=True 走 Celery worker）。"""
    if _settings.celery_enabled:
        from app.tasks import task_meeting_transcribe

        task_meeting_transcribe.delay(meeting_id)
        return
    _MEETING_TASKS[meeting_id] = asyncio.get_event_loop().create_task(_run_meeting(meeting_id))


async def _run_meeting(meeting_id: str) -> None:
    """转写主流程：时长校验 → 复用 transcriber（FunASR 本地）→ [mm:ss] 全文落盘 → ready。"""
    logger.info("会议转写任务启动 meeting=%s", meeting_id)
    engine = get_global_engine()
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT server_path, sys_path, title FROM meeting_recordings WHERE id=:id"), {"id": meeting_id}
                )
            ).first()
        if row is None:
            logger.warning("会议录音不存在（已删除）meeting=%s", meeting_id)
            return
        audio_path, sys_path, title = row

        duration = await asyncio.to_thread(_probe_duration, audio_path)
        if duration > _settings.meeting_max_duration_min * 60:
            raise ValueError(f"录音时长 {int(duration) // 60} 分 {int(duration) % 60} 秒，超过上限 {_settings.meeting_max_duration_min} 分钟")

        # 共用 FunASR 转写管线（内部 _funasr_lock 全局串行；对纯音频文件 ffmpeg 直接抽 16k wav）
        # 2026-08-25 双轨：sys_path 存在 → 本地/线上分轨转写 + 说话人聚类（本地A/线上B 标签）；
        # 单轨（上传已有音频）→ 聚类增强：多说话人标「说话人A/B」，单说话人保持原格式
        # 2026-08-27（整体超时兜底）：FunASR 挂起会持 _funasr_lock 卡全局转写——
        # wait_for 超时取消任务，async with 锁在 finally 自动释放，后续转写恢复
        from app.services.transcriber import diarize_segments, transcribe_dual_track

        async def _do_transcribe():
            if sys_path:
                tr = await transcribe_dual_track(audio_path, sys_path)
                return tr, tr.get("text") or ""
            tr = await transcribe(audio_path)
            segs = await diarize_segments(audio_path, tr.get("segments") or [])
            speakers = {s.get("speaker") for s in segs if s.get("speaker") is not None}
            if len(speakers) > 1:
                text = "\n".join(
                    f"[{s.get('start', '00:00')}] 说话人{chr(ord('A') + s['speaker'])}: {s.get('text', '')}"
                    for s in segs)
            elif tr.get("segments"):
                text = "\n".join(
                    f"[{s.get('start', '00:00')}] {s.get('text', '')}" for s in segs)
            else:
                text = ""
            return tr, text

        tr, transcript_text = await asyncio.wait_for(_do_transcribe(), _TRANSCRIBE_TIMEOUT_S)
        if sys_path:
            # 2026-08-25（用户要求）：合成双轨为一个可听音频（amix 保留长轨时长）→ zip 下载含「完整音频」
            try:
                merged_path = await _merge_tracks(audio_path, sys_path)
                async with engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE meeting_recordings SET merged_path=:p, updated_at=NOW() WHERE id=:id"),
                        {"p": str(merged_path), "id": meeting_id},
                    )
            except Exception as e:
                logger.warning("双轨合成失败（zip 不含完整音频）meeting=%s err=%s", meeting_id, str(e)[:150])

        # 2026-08-27（脏字符防护）：FunASR 转写文本可能含 NUL/非法 surrogate → PG UTF8 拒绝；
        # 写 .md 与 DB 统一用清洗后值（.md 供下载，同值一致）
        transcript_text = sanitize_db_text(transcript_text or "")
        item_dir = _meeting_dir(meeting_id)
        item_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = item_dir / "语音转写.md"
        await asyncio.to_thread(transcript_path.write_text, transcript_text or "（未检测到语音内容）", encoding="utf-8")

        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE meeting_recordings SET status='ready', transcript=:t, transcript_path=:p, "
                     "duration_s=:d, error_msg=NULL, updated_at=NOW() WHERE id=:id"),
                {"t": transcript_text, "p": str(transcript_path), "d": duration, "id": meeting_id},
            )
        logger.info("会议录音转写完成 meeting=%s len=%d backend=%s", meeting_id, len(transcript_text), tr.get("backend"))
    except Exception as e:
        # 2026-08-27：超时（wait_for TimeoutError）给友好提示——录音文件仍在，可点击重新转写
        if isinstance(e, asyncio.TimeoutError):
            err_msg = f"转写超时（>{_TRANSCRIBE_TIMEOUT_S // 60} 分钟），可能音频损坏或服务卡住，可点击重新转写"
        else:
            err_msg = str(e)[:300]
        logger.warning("会议录音转写失败 meeting=%s err=%s", meeting_id, err_msg[:200])
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE meeting_recordings SET status='failed', error_msg=:m, updated_at=NOW() WHERE id=:id"),
                {"m": err_msg, "id": meeting_id},
            )


async def _run_summary(meeting_id: str, scene: str, custom_prompt: str, aux_model: dict | None = None) -> None:
    """LLM 总结主流程：场景/自定义提示词 + 转写稿（预算截断注入）→ 总结.md 落盘 → done。
    aux_model 可选：机器人级/前端选择的总结模型覆盖（优先于 llm_aux meeting 档）。"""
    engine = get_global_engine()
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT transcript, transcript_path, server_path, dept_id, user_role "
                         "FROM meeting_recordings WHERE id=:id"), {"id": meeting_id}
                )
            ).first()
        if row is None:
            logger.warning("会议录音不存在（已删除）meeting=%s", meeting_id)
            return
        transcript_text = row.transcript or ""
        if not transcript_text:
            raise ValueError("转写稿为空，无法生成总结")

        # 2026-08-27（用户要求）：场景预设 + 用户额外要求并存——选场景时附加要求追加到 user 侧
        # （system 保留场景专用提示词；custom_prompt 不再顶替场景）
        scene_cfg = MEETING_SCENES.get(scene) if scene else None
        if not scene_cfg and custom_prompt.strip():
            system = CUSTOM_SUMMARY_SYSTEM
            user = f"用户总结要求：{custom_prompt.strip()}\n\n语音转写稿：\n{truncate_head_tail(transcript_text, _settings.meeting_summary_inject_chars)}"
        else:
            if not scene_cfg:
                raise ValueError(f"未知总结场景: {scene}")
            system = scene_cfg["prompt"]
            extra = f"\n\n用户额外要求（优先级高于场景预设）：{custom_prompt.strip()}" if custom_prompt.strip() else ""
            user = f"语音转写稿：\n{truncate_head_tail(transcript_text, _settings.meeting_summary_inject_chars)}{extra}"

        from app.agent.llm_client import create_text_client

        # 2026-08-25：前端选择的总结模型优先（aux_model）→ llm_aux meeting 档 → 默认
        cfg = aux_model or await _aux_cfg_for_meeting(row.dept_id, row.user_role)
        client, model, tkw = create_text_client(cfg)
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                **tkw,
            ),
            timeout=_settings.llm_call_timeout_seconds,
        )
        summary = (resp.choices[0].message.content or "").strip()

        item_dir = Path(row.transcript_path).parent if row.transcript_path else _meeting_dir(meeting_id)
        # 2026-09-01：mkdir 兜底——总结是后台任务，若期间目录被清理（测试/删除竞态）仍可落盘
        item_dir.mkdir(parents=True, exist_ok=True)
        summary_path = item_dir / "总结.md"
        await asyncio.to_thread(summary_path.write_text, summary, encoding="utf-8")
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE meeting_recordings SET status='done', summary=:s, scene=:sc, custom_prompt=:cp, "
                     "summary_path=:p, error_msg=NULL, updated_at=NOW() WHERE id=:id"),
                {"s": summary, "sc": scene, "cp": custom_prompt.strip() or None, "p": str(summary_path), "id": meeting_id},
            )
        logger.info("会议录音总结完成 meeting=%s scene=%s len=%d", meeting_id, scene, len(summary))
    except Exception as e:
        logger.warning("会议录音总结失败 meeting=%s err=%s", meeting_id, str(e)[:200])
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE meeting_recordings SET status='failed', error_msg=:m, updated_at=NOW() WHERE id=:id"),
                {"m": str(e)[:300], "id": meeting_id},
            )


async def _aux_cfg_for_meeting(dept_id: str, role: str) -> dict | None:
    """llm_aux 档模型配置（任务 key meeting；usage 未勾选该任务则回退默认模型）。"""
    from app.services.config_service import get_aux_model_cfg_for_task

    return await get_aux_model_cfg_for_task(dept_id, role, "meeting")


async def _merge_tracks(local_path: str, sys_path: str) -> Path:
    """双轨合成一个可听音频（amix 混合，保留长轨时长；输出 m4a AAC，zip 下载用）。"""
    import subprocess as _sp

    item_dir = Path(local_path).parent
    merged_path = item_dir / "合成音频.m4a"
    await asyncio.to_thread(
        _sp.run,
        ["ffmpeg", "-y", "-v", "error", "-i", local_path, "-i", sys_path,
         "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0",
         "-c:a", "aac", "-b:a", "128k", str(merged_path)],
        capture_output=True, text=True, timeout=600,
    )
    if not merged_path.exists():
        raise RuntimeError("ffmpeg 合成未产出文件")
    logger.info("双轨合成完成 %s", merged_path)
    return merged_path


def summarize_meeting(meeting_id: str, scene: str, custom_prompt: str, aux_model: dict | None = None) -> None:
    """后台启动总结（fire-and-forget；celery 分支同转写）。"""
    if _settings.celery_enabled:
        from app.tasks import task_meeting_summarize

        task_meeting_summarize.delay(meeting_id, scene, custom_prompt, aux_model)
        return
    _MEETING_TASKS[meeting_id] = asyncio.get_event_loop().create_task(
        _run_summary(meeting_id, scene, custom_prompt, aux_model))


async def recover_stale_meetings() -> int:
    """服务重启恢复：_MEETING_TASKS 是进程内存（重启即丢），中断状态重置为 failed（可重新上传）。"""
    engine = get_global_engine()
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text("SELECT id FROM meeting_recordings WHERE status IN ('uploaded','transcribing','summarizing')")
            )
        ).all()
        for r in rows:
            await conn.execute(
                text("UPDATE meeting_recordings SET status='failed', error_msg='服务重启，录音任务已中断', "
                     "updated_at=NOW() WHERE id=:id"), {"id": r.id},
            )
    if rows:
        logger.warning("重启恢复：%s 个中断会议录音任务已重置为 failed", len(rows))
    return len(rows)


async def cleanup_meeting_data() -> dict:
    """会议录音数据清理（每日 03:45 随 _job_tools_cleanup 调用）：
    - 原始音频超过 video_cache_ttl_days=1 天删除（目录内 .wav/.webm/.m4a 等音频文件）
    - 文本产物（转写/总结）与 DB 行保留至 video_text_ttl_days=30 天，之后整体删除
    """
    import shutil

    engine = get_global_engine()
    deleted_db = 0
    deleted_dirs = 0
    cache_files = 0
    async with engine.begin() as conn:
        # 阶段 1：音频缓存清理（1 天 < age <= 30 天，删音频文件，保留文本与 DB）
        cache_rows = (
            await conn.execute(
                text("SELECT id, server_path FROM meeting_recordings "
                     "WHERE server_path IS NOT NULL AND created_at < NOW() - :c_interval "
                     "AND created_at >= NOW() - :t_interval"),
                {"c_interval": f"interval '{_settings.meeting_audio_ttl_days} days'",
                 "t_interval": f"interval '{_settings.meeting_text_ttl_days} days'"},
            )
        ).all()
        for r in cache_rows:
            p = Path(r.server_path)
            if p.exists():
                p.unlink(missing_ok=True)
                cache_files += 1
        # 阶段 2：文本产物过期（>30 天）整体删除（DB + 目录）
        old_rows = (
            await conn.execute(
                text("SELECT id FROM meeting_recordings WHERE created_at < NOW() - :t_interval"),
                {"t_interval": f"interval '{_settings.meeting_text_ttl_days} days'"},
            )
        ).all()
        for r in old_rows:
            await conn.execute(text("DELETE FROM meeting_recordings WHERE id=:id"), {"id": r.id})
            deleted_db += 1
    for r in old_rows:
        d = _meeting_dir(r.id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            deleted_dirs += 1
    logger.info("会议录音清理：缓存音频 %d、过期录音 %d 条 / %d 目录", cache_files, deleted_db, deleted_dirs)
    return {"cache_files": cache_files, "deleted_db": deleted_db, "deleted_dirs": deleted_dirs}
