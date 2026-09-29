"""对话 API：会话 CRUD / SSE 问答流 / 确认唤醒 / 文件上传。

SSE 协议（POST /chat/ask）：
- text: 流式文本增量 {"delta"}
- tool: 工具进度 {"tool_name","status","detail"}
- chart: 图表产出 {"chart_id","round_id","label","type","option"}
- output: 轮次产出物汇总 {"round_id","outputs"}
- confirm: 需用户确认 {"session_id","round_id","plan","expires_at"}
- done: 本轮结束 {"message","round_id","outputs"}
- error: 终止性错误 {"code","message"}
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, insert, select, text, update

from app.core.config import get_settings
from app.core.database import get_global_engine
from app.core.exceptions import app_error
from app.core.file_utils import check_magic_bytes, sanitize_err_text, sanitize_filename, save_limited
from app.core.logging import get_logger
from app.core.middleware import get_business_user, get_client_id, get_current_user
from app.core.rate_limit import rate_limit_dep
from app.core.url_utils import output_url
from app.api.skills import DEFAULT_TOOLS
from app.models import ChatFile, ChatMessage, ChartOutput, Session
from app.services.config_service import get_upload_limits
from app.services.chat_service import (
    UPGRADE_QUESTION, legacy_stream_ask, replay_stream, start_bg_task, tail_stream,
)
from app.services.interaction_waiter import get_plan_waiter, get_question_waiter

# 2026-09-08（续跑）：中断后"继续"类独立指令（整句仅此意图，防误伤长句中的"继续说/往下看"）
_RESUME_RE = re.compile(r"^\s*(继续|接着(做|干|弄|来)?|往下(做|弄|干)?|续(做|跑)?|继续完成|继续做)[\s。！!？?]*$")
from app.services.task_registry import get_task, unregister

logger = get_logger("api.chat")

_settings = get_settings()
router = APIRouter(prefix="/chat", tags=["chat"])

# 会话文件白名单（html/pptx/ppt/md 供 Agent 读取模板与文档——file_parse 走 markitdown 解析；
# 2026-08-21：.zip 支持——上传自动解压到同目录子文件夹，zip_extract 工具可二次解压）
ALLOWED_TYPES = {".xlsx", ".xls", ".csv", ".docx", ".pdf", ".png", ".jpg", ".jpeg",
                 ".html", ".htm", ".pptx", ".ppt", ".md", ".txt", ".zip"}
# 2026-09-15（媒体工具 video_understand/audio_transcribe）：音视频上传白名单——
# 体积上限另计（media_upload_max_mb=200）且走流式落盘（不整读进内存），魔数校验见 file_utils
MEDIA_TYPES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".flv", ".wmv", ".3gp",
               ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".amr"}
ALLOWED_TYPES |= MEDIA_TYPES


class CreateSessionRequest(BaseModel):
    client_id: str


class AskRequest(BaseModel):
    session_id: str | None = None
    question: str = ""
    active_skills: list[str] | None = None   # 2026-09-15：None=调用方未指定 → 回退默认工具集（见 handler）；
                                             # 显式 [] 仍表示"一个技能都不启用"
    auto_skill: bool = False
    multi_table: bool = False
    file_ids: list[str] = []
    # v2 交互框架（2026-08-14）
    mode: Literal["quick", "complex"] | None = None   # None=沿用会话模式（新会话默认 quick）
    upgrade: bool = False                              # 快速→复杂升级（P0：question 可空，后端替换衔接文本）
    thinking: Literal["off", "low", "high", "max"] | None = None  # D11/D3：思考强度自选（None=按模式默认；三档 low/high/max 与 admin 配置页对齐）
    aux_overrides: dict | None = None                  # D3：辅助模型覆盖（技能卡配置，批次 6 消费）
    # 任务后台化（2026-08-18）：reconnect=断线续播请求（不分配新 round、不启动新任务），
    # last_seq=前端已消费到的事件序号（seq=Redis 事件流 index），回放 (last_seq+1..] 增量
    reconnect: bool = False
    last_seq: int | None = None

    # 空/纯空白问题直接拒绝（并发压测发现：空问题曾返回 200 浪费 LLM 调用）；
    # 唯一例外：upgrade 升级请求（question 可空，后端替换为固定衔接文本）
    @field_validator("question")
    @classmethod
    def validate_question(cls, v: str) -> str:
        return v


# D27（2026-08-10）：round_id 分配改 sessions.last_round 原子自增（UPDATE...RETURNING），
# 分配与落库解耦（persist 失败不再复用同一 round_id）。
# 原 _ROUND_LOCKS 每会话永久泄漏一个 Lock 的问题随之消失（原子 UPDATE 天然串行）。
# round_id 允许空洞（前端 Map 键/outputs URL 均容忍非连续）。


def _is_uuid(s: str) -> bool:
    """M24：UUID 格式校验（非 UUID 字符串直接 422，防 asyncpg DataError → 500 + SQL 泄露）。"""
    try:
        uuid.UUID(s)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _sanitize_outputs_for_client(outputs, round_id: int) -> list | None:
    """N3（红队二次扩展，2026-08-18）：响应层脱敏——outputs 中的服务器绝对路径
    （/data/outputs/...）重写为可下载的相对 URL。

    DB 存储**不动**（chat_service._load_history 需服务器路径注入 LLM 上下文供 read_output 读回）；
    仅 API 边界（get_messages）对外脱敏，防服务器路径信息外泄。解析失败兜底保留原值（不抛异常）。
    """
    if not isinstance(outputs, list):
        return outputs
    out_dir = str(_settings.output_dir)
    cleaned: list = []
    for o in outputs:
        if isinstance(o, dict) and isinstance(o.get("file_path"), str):
            fp = o["file_path"]
            if fp.startswith(out_dir):
                try:
                    rel = Path(fp).relative_to(_settings.output_dir)
                    parts = rel.parts
                    if len(parts) >= 3:  # {session_id}/{round_id}/{filename}
                        o = {**o, "file_path": output_url(parts[0], parts[1], "/".join(parts[2:]))}
                except ValueError:
                    pass  # 非 output_dir 内路径或解析失败：兜底保留原值
        cleaned.append(o)
    return cleaned


def _sanitize_files_for_client(files) -> list | None:
    """F-03（红队四次，2026-08-19）：响应层脱敏——files 中的服务器绝对路径
    （/data/uploads/users/...）改返纯文件名。

    前端仅消费 file_name 展示（ChatPanel 徽章）；file_path 无下载用途（会话文件
    浮窗走独立接口 GET /chat/sessions/{id}/files）。绝对路径一律脱敏防服务器文件
    系统结构泄露；非绝对路径（相对/URL 形态）兜底保留原值。
    """
    if not isinstance(files, list):
        return files
    cleaned: list = []
    for f in files:
        if isinstance(f, dict) and isinstance(f.get("file_path"), str):
            fp = f["file_path"]
            if fp.startswith("/"):
                f = {**f, "file_path": Path(fp).name}
        cleaned.append(f)
    return cleaned


async def _next_round(engine, session_id: str) -> int:
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("UPDATE sessions SET last_round = last_round + 1 WHERE id=:sid RETURNING last_round"),
                {"sid": session_id},
            )
        ).first()
    return row[0] if row and row[0] else 1


@router.post("/sessions")
async def create_session(req: CreateSessionRequest, user: dict = Depends(get_business_user)):
    engine = get_global_engine()
    async with engine.begin() as conn:
        sid = str(uuid.uuid4())
        await conn.execute(
            insert(Session).values(
                id=sid,
                user_id=user["user_id"],
                client_id=req.client_id,
                title="新会话",
                department_id=user["dept_id"],
            )
        )
    # BUG-02：契约统一——响应同时含 id（标准字段）与 session_id（历史兼容）
    return {"id": sid, "session_id": sid, "title": "新会话"}


@router.get("/sessions")
async def list_sessions(client_id: str | None = None, user: dict = Depends(get_business_user)):
    engine = get_global_engine()
    async with engine.connect() as conn:
        q = (
            select(Session)
            .where(Session.user_id == user["user_id"])
            .order_by(Session.last_activity_at.desc())
            .limit(50)
        )
        if client_id:
            q = q.where(Session.client_id == client_id)
        rows = (await conn.execute(q)).all()
    return [
        {
            "id": r.id,
            "title": r.title,
            "last_activity_at": r.last_activity_at.isoformat() if r.last_activity_at else None,
            "is_readonly": r.is_readonly,
            # B8（D20）：plan 状态（前端会话列表/提示条用）
            "plan_status": r.plan_status,
            "plan_expires_at": r.plan_expires_at.isoformat() if r.plan_expires_at else None,
            # v2：双模式（会话级记住；quick/complex）
            "mode": getattr(r, "mode", "quick"),
            # 需求 3：token 计费（deepseek 平台累计；0/None=无计费信息，前端显示未知）
            "cost_tokens": getattr(r, "cost_tokens", 0) or 0,
            # 2026-08-17（价格换算）：细分（缓存命中/未命中输入 + 输出）+ 模型名（flash/pro 价格不同）
            "cost_prompt_hit": getattr(r, "cost_prompt_hit", 0) or 0,
            "cost_prompt_miss": getattr(r, "cost_prompt_miss", 0) or 0,
            "cost_completion": getattr(r, "cost_completion", 0) or 0,
            "cost_model": getattr(r, "cost_model", None),
        }
        for r in rows
    ]


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str, user: dict = Depends(get_business_user)):
    if not _is_uuid(session_id):  # E-03(API)：非 UUID 直接 422（防 asyncpg DataError → 500）
        raise app_error("E011", "非法的会话 ID", status_code=422)
    engine = get_global_engine()
    async with engine.connect() as conn:
        sess = (await conn.execute(select(Session).where(Session.id == session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "会话不存在或无权访问", status_code=404)
        rows = (
            await conn.execute(
                select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at)
            )
        ).all()
        # 图表完整 option（历史会话预览区重建用）
        charts = (
            await conn.execute(
                select(ChartOutput).where(ChartOutput.session_id == session_id).order_by(ChartOutput.created_at)
            )
        ).all()
    return {
        # B8（D20）：plan 状态注入（前端 pending 提示条）；v2：mode/plan_json
        "session": {"id": sess.id, "title": sess.title, "is_readonly": sess.is_readonly,
                    "plan_status": sess.plan_status,
                    "plan_expires_at": sess.plan_expires_at.isoformat() if sess.plan_expires_at else None,
                    "mode": getattr(sess, "mode", "quick"),
                    "plan_json": getattr(sess, "plan_json", None)},
        "messages": [
            {
                "role": r.role,
                "content": r.content,
                "round_id": r.round_id,
                "outputs": _sanitize_outputs_for_client(r.outputs, r.round_id),
                "tool_events": r.tool_events,
                "files": _sanitize_files_for_client(r.files),
            }
            for r in rows
        ],
        "charts": [
            {
                "id": c.id,                       # PNG 导出接口（/chat/charts/{id}/png）需要
                "round_id": c.round_id,
                "label": c.chart_label,
                "chart_type": c.chart_type,
                "option": c.option_json,
            }
            for c in charts
        ],
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: dict = Depends(get_business_user)):
    if not _is_uuid(session_id):  # M24
        raise app_error("E011", "非法的会话 ID", status_code=422)
    engine = get_global_engine()
    async with engine.begin() as conn:
        sess = (await conn.execute(select(Session).where(Session.id == session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "会话不存在或无权访问", status_code=404)
        await conn.execute(delete(Session).where(Session.id == session_id))
    # 任务后台化（2026-08-18）：删除会话时终止运行中任务 + 清事件流/中断队列
    rec = get_task(session_id)
    if rec is not None:
        rec.terminated.set()
        if rec.agent_task is not None:
            rec.agent_task.cancel()
        unregister(rec)
    from app.core.redis import redis_delete

    await redis_delete(f"sse_events:{session_id}", f"interrupt:{session_id}")
    # 2026-09-10（缓存核查）：历史摘要缓存按会话前缀留成孤儿键（原靠 21 天 TTL 自然过期）
    try:
        from app.core.redis import redis_scan_delete

        await redis_scan_delete(f"hist_sum:v1:{session_id}:*")
    except Exception:
        pass
    # M23：物理清理（原仅删 DB 行，产出/上传目录孤儿残留永不被 cleanup 清理）
    out_dir = Path(f"{_settings.output_dir}/{session_id}")
    if out_dir.exists():
        await asyncio.to_thread(shutil.rmtree, out_dir, True)
    for sub in Path(_settings.upload_dir).glob(f"users/*/{session_id}"):
        if sub.is_dir():
            await asyncio.to_thread(shutil.rmtree, sub, True)
    return {}


@router.post("/ask")
async def chat_ask(
    req: AskRequest,
    user: dict = Depends(get_business_user),
    client_id: str | None = Depends(get_client_id),
    _rl: None = Depends(rate_limit_dep("ask", _settings.rate_limit_ask_per_min, by="user")),  # SEC-05
):
    if req.session_id and not _is_uuid(req.session_id):  # M24
        raise app_error("E011", "非法的会话 ID", status_code=422)

    # 任务后台化（2026-08-18）：reconnect 请求 = 断线续播/回放——不分配新 round、
    # 不启动新任务。任务 running → tail_stream 回放增量+尾随；已结束 → 回放剩余
    # （空流 EOF 让前端 onStale 兜底重拉 get_messages）。
    if _settings.task_bg_enabled and req.reconnect:
        if not req.session_id:
            raise app_error("E011", "会话 ID 不能为空", status_code=422)
        async with get_global_engine().connect() as conn:
            sess = (await conn.execute(select(Session).where(Session.id == req.session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "会话不存在或无权访问", status_code=404)
        last_seq = req.last_seq if req.last_seq is not None else -1
        rec = get_task(req.session_id)
        logger.info("chat/ask 重连 session=%s last_seq=%d", req.session_id, last_seq)
        gen = tail_stream(rec, last_seq) if rec is not None and rec.status == "running" \
            else replay_stream(req.session_id, last_seq)
        return StreamingResponse(gen, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 2026-09-15（工具可见性）：API 直连不传 active_skills 时回退默认工具集（与 GET /skills/prefs 无记录一致）——
    # 原默认 [] 会得到"只有恒注入工具"的会话（9-11 工具可见性验证脚本实测：问业务库表结构得到"需管理员开通"的误答）
    active_skills = req.active_skills
    if active_skills is None:
        active_skills = [] if req.auto_skill else list(DEFAULT_TOOLS)

    # 空问题拒绝（升级请求除外——question 可空，后端替换固定衔接文本）
    if not req.upgrade and (not req.question or not req.question.strip()):
        raise app_error("E011", "问题不能为空", status_code=422)
    engine = get_global_engine()
    session_mode = "quick"
    # 归属三态（2026-08-24）：新会话复制的知识库 xlsx（仅新会话分支填充）+ 截断标记
    kb_xlsx_files: list = []
    kb_xlsx_truncated = False
    if req.session_id:
        async with engine.connect() as conn:
            sess = (await conn.execute(select(Session).where(Session.id == req.session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "会话不存在或无权访问", status_code=404)
        session_id = sess.id
        session_mode = getattr(sess, "mode", "quick") or "quick"
        # 首次提问时更新会话标题（默认"新会话" → 取提问前 30 字）
        if not sess.title or sess.title == "新会话":
            async with engine.begin() as conn:
                await conn.execute(
                    update(Session).where(Session.id == session_id).values(title=req.question[:30])
                )
    else:
        # 新会话：必须用 begin()（connect() 上下文退出会自动回滚未 commit 的 INSERT）
        session_id = str(uuid.uuid4())
        async with engine.begin() as conn:
            await conn.execute(
                insert(Session).values(
                    id=session_id,
                    user_id=user["user_id"],
                    client_id=client_id or "",
                    title=req.question[:30],
                    department_id=user["dept_id"],
                )
            )
        # 归属三态（2026-08-24）：新会话复制知识库 xlsx 到会话可读目录（失败不阻断会话创建；
        # 旧会话不追补、断线重连不重注入——降级文档化）
        try:
            from app.services.kb_service import copy_kb_xlsx_to_session

            copied = await copy_kb_xlsx_to_session(user["user_id"], user["dept_id"], session_id)
            kb_xlsx_files = copied.get("files", [])
            kb_xlsx_truncated = bool(copied.get("truncated"))
        except Exception as e:
            logger.warning("知识库 xlsx 会话复制失败 session=%s: %s", session_id[:8], str(e)[:150])

    # v2 双模式解析：显式 mode 优先；None 沿用会话模式；升级请求强制 complex。
    # 只许快速→复杂升级，不可降级（设计书 §5.1）。
    mode = "complex" if req.upgrade else (req.mode or session_mode)
    if session_mode == "complex" and mode == "quick" and not req.upgrade:
        raise app_error("E011", "复杂任务模式不支持降级", status_code=400)
    if mode == "complex" and session_mode != "complex":
        async with engine.begin() as conn:
            await conn.execute(update(Session).where(Session.id == session_id).values(mode="complex"))
    question = req.question.strip() or UPGRADE_QUESTION

    # 2026-09-08（续跑）："继续/接着做/往下做"类独立指令 → 中断恢复注记强化为"直接接续"
    # （_load_history 消费 interrupted 标记时按 resume_hint 定制引导；无标记时纯历史注入也够用）
    resume_hint = bool(_RESUME_RE.match(question))

    # 任务后台化：同会话已有 running 任务 → 409（前端 streaming 态下发消息走 /interrupt
    # 插话路径——继续修改/执行不取消任务，正常不可达；此防御防并发直连 API）
    if _settings.task_bg_enabled and get_task(session_id) is not None:
        raise app_error("E011", "该会话已有任务在运行，请等待完成或先停止", status_code=409)

    # 2026-09-15（压缩期间不能对话）：该会话正在压缩 → 409（压缩是独占回合；锁由压缩方持有）
    from app.services.context_service import compacting as _ctx_compacting

    if await _ctx_compacting(session_id):
        raise app_error("E011", "正在压缩上下文，请稍候再发送", status_code=409)

    round_id = await _next_round(engine, session_id)
    # 2026-09-10（同类排查）：续跑日志原先在 round_id 赋值**之前**，引用未定义变量 →
    # 任何"继续/接着做/往下做"的消息都会 NameError 500（pyflakes undefined name 抓出）。
    if resume_hint:
        logger.info("续跑触发词命中 session=%s round=%d", session_id, round_id)
    logger.info("chat/ask 开始 session=%s round=%d user=%s mode=%s upgrade=%s skills=%s auto=%s",
                session_id, round_id, user["username"], mode, req.upgrade, active_skills, req.auto_skill)

    if not _settings.task_bg_enabled:
        # 回退路径（task_bg_enabled=False）：任务绑定请求生命周期，断连即取消（旧行为）
        # ②（全局 Explore 排查）：legacy 无任务注册 → 同会话并发 ask 双 graph 竞态——
        # 进程级 in-flight 标记（流结束 unregister）
        from app.services.task_registry import TaskRecord as _TR
        from app.services.task_registry import get_task as _get_task
        from app.services.task_registry import register as _register
        from app.services.task_registry import unregister as _unregister

        if _get_task(session_id) is not None:
            raise app_error("E011", "该会话已有任务在运行，请等待完成或先停止", status_code=409)
        _legacy_rec = _TR(session_id=session_id, round_id=round_id, question=question,
                          broadcaster=None)  # type: ignore[arg-type]
        _register(_legacy_rec)

        async def event_stream():
            try:
                async for frame in legacy_stream_ask(
                    session_id=session_id,
                    round_id=round_id,
                    question=question,
                    user=user,
                    active_skills=active_skills,
                    auto_skill=req.auto_skill,
                    multi_table=req.multi_table,
                    client_id=client_id or "",
                    file_ids=req.file_ids,
                    mode=mode,
                    upgrade=req.upgrade,
                    thinking=req.thinking,
                    aux_overrides=req.aux_overrides,
                    kb_xlsx_files=kb_xlsx_files,
                    kb_xlsx_truncated=kb_xlsx_truncated,
                    resume_hint=resume_hint,
                ):
                    yield frame
            finally:
                _unregister(_legacy_rec)
            logger.info("chat/ask 结束 session=%s round=%d", session_id, round_id)

        return StreamingResponse(event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 后台化主路径：任务注册 + 首连转发（last_seq=-1 → 回放全量，含 mode seq=0）
    rec = await start_bg_task(
        session_id=session_id,
        round_id=round_id,
        question=question,
        user=user,
        active_skills=active_skills,
        auto_skill=req.auto_skill,
        multi_table=req.multi_table,
        client_id=client_id or "",
        file_ids=req.file_ids,
        mode=mode,
        upgrade=req.upgrade,
        thinking=req.thinking,
        aux_overrides=req.aux_overrides,
        kb_xlsx_files=kb_xlsx_files,
        kb_xlsx_truncated=kb_xlsx_truncated,
        resume_hint=resume_hint,
    )
    return StreamingResponse(tail_stream(rec, -1), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class AnswerRequest(BaseModel):
    session_id: str
    question_id: str
    answers: list[dict] = []      # [{index:int, selected:[int], other_text:str}]
    extra_text: str = ""


@router.post("/answer")
async def chat_answer(req: AnswerRequest, user: dict = Depends(get_business_user)):
    """v2：反问浮窗回答（唤醒 QuestionWaiter；超时按推荐项自动提交，此处仅用户主动回答通道）。"""
    if not _is_uuid(req.session_id):
        raise app_error("E011", "非法的会话 ID", status_code=422)
    async with get_global_engine().connect() as conn:
        sess = (await conn.execute(select(Session).where(Session.id == req.session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "会话不存在或无权访问", status_code=404)
    ok = get_question_waiter().answer(req.session_id, req.question_id, req.answers,
                                      req.extra_text.strip()[:500])
    if not ok:
        raise app_error("E009", "回答已过期或不存在", status_code=404)
    return {"ok": True}


class PlanApproveRequest(BaseModel):
    session_id: str
    plan_id: str
    decision: Literal["approve", "reject"]
    feedback: str = ""  # 不同意时必填意见（修订依据）


@router.post("/plan-approve")
async def chat_plan_approve(req: PlanApproveRequest, user: dict = Depends(get_business_user)):
    """v2：计划批准卡（唯一人工门）。唤醒 PlanWaiter；waiter 不存在时走持久化重开路径。"""
    if not _is_uuid(req.session_id):
        raise app_error("E011", "非法的会话 ID", status_code=422)
    engine = get_global_engine()
    async with engine.connect() as conn:
        sess = (await conn.execute(select(Session).where(Session.id == req.session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "会话不存在或无权访问", status_code=404)
        pending_json = getattr(sess, "plan_json", None) if req.decision == "approve" else None
    # 决策值归一化（旧 /chat/confirm 约定）：approve→approved / reject→rejected——
    # plan_approval 节点分支按 approved/rejected/timeout 判定（踩坑：直接透传导致全部落 timeout 分支）
    decision = "approved" if req.decision == "approve" else "rejected"
    ok = get_plan_waiter().approve(req.session_id, req.plan_id, decision,
                                   req.feedback.strip()[:500])
    if ok:
        return {"ok": True}
    # 等待器不存在（批准卡超时/服务重启后卡片仍在）→ DB pending 计划按 plan_id 匹配原子确认
    # （下个 ask 走 plan_status=confirmed 恢复路径执行；前端提示"发送任意消息开始执行"）
    if req.decision == "approve" and isinstance(pending_json, dict) \
            and pending_json.get("plan_id") == req.plan_id:
        async with engine.begin() as conn:
            r = await conn.execute(
                text("UPDATE sessions SET plan_status='confirmed' "
                     "WHERE id=:s AND plan_status='pending' AND plan_json->>'plan_id'=:p"),
                {"s": req.session_id, "p": req.plan_id},
            )
        if r.rowcount:
            return {"ok": True, "deferred": True}
    raise app_error("E009", "批准已过期或不存在", status_code=404)


class InterruptRequest(BaseModel):
    session_id: str
    message: str


@router.post("/interrupt")
async def chat_interrupt(req: InterruptRequest, user: dict = Depends(get_business_user)):
    """v2：中途插话（流式中发送入中断队列，下一回合边界由 agent_llm 消费）。

    无活动流时也接受——内容留待下个 ask 回合消费。
    """
    if not _is_uuid(req.session_id):
        raise app_error("E011", "非法的会话 ID", status_code=422)
    if not req.message or not req.message.strip():
        raise app_error("E011", "插话内容不能为空", status_code=422)
    # SEC-06：interrupt 通道此前绕过入口注入检测（文本以【系统提醒】进 LLM 上下文）——
    # 写入端过 InputFilter，与 /chat/ask 入口同码拦截（E010）
    from app.core.input_filter import InputFilter

    filt = InputFilter.check(req.message.strip(), user["dept_id"], role=user["role"])
    if not filt["passed"]:
        raise app_error("E010", f"插话内容未通过安全校验：{filt['reason']}", status_code=400)
    async with get_global_engine().connect() as conn:
        sess = (await conn.execute(select(Session).where(Session.id == req.session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "会话不存在或无权访问", status_code=404)
    from app.core.redis import redis_rpush

    payload = {"type": "user", "message": req.message.strip()[:500]}
    await redis_rpush(f"interrupt:{req.session_id}", json.dumps(payload, ensure_ascii=False))
    # 2026-08-20（P1-④）：插话入队日志——走查时段无任何插话痕迹是排查最大障碍
    # （端点与消费两端都零日志，无法证明 POST 是否到达）
    logger.info("chat/interrupt 入队 session=%s len=%d", req.session_id[:8], len(req.message.strip()))
    return {"ok": True}


class StopRequest(BaseModel):
    session_id: str


@router.post("/stop")
async def chat_stop(req: StopRequest, user: dict = Depends(get_business_user)):
    """任务后台化（2026-08-18）：显式停止运行中任务（区别于断连——断连任务继续）。

    取消 agent_task → _agent_runner finally 放 END_MARKER → relay 收尾：
    写 aborted 事件（入流 + 广播，活跃转发器/重连回放均能收到）+ 降级落库。
    """
    if not _is_uuid(req.session_id):
        raise app_error("E011", "非法的会话 ID", status_code=422)
    if not _settings.task_bg_enabled:
        raise app_error("E011", "任务后台化未启用", status_code=400)
    async with get_global_engine().connect() as conn:
        sess = (await conn.execute(select(Session).where(Session.id == req.session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "会话不存在或无权访问", status_code=404)
    rec = get_task(req.session_id)
    if rec is None or rec.status != "running" or rec.agent_task is None:
        raise app_error("E011", "当前没有运行中的任务", status_code=409)
    rec.terminated.set()
    rec.agent_task.cancel()
    logger.info("chat/stop session=%s round=%d", req.session_id, rec.round_id)
    return {"ok": True}


@router.get("/sessions/{session_id}/status")
async def session_status(session_id: str, user: dict = Depends(get_business_user)):
    """任务后台化（2026-08-18）：会话任务状态（前端刷新后恢复 streaming 态 / 中断提示条用）。

    running 时返回当前事件序号 last_seq（前端可跳读增量）；否则返回 DB task_status。
    """
    if not _is_uuid(session_id):
        raise app_error("E011", "非法的会话 ID", status_code=422)
    if not _settings.task_bg_enabled:
        return {"task_status": "none", "running": False, "last_seq": -1,
                "round_id": None, "question": None}
    async with get_global_engine().connect() as conn:
        sess = (await conn.execute(select(Session).where(Session.id == session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "会话不存在或无权访问", status_code=404)
    rec = get_task(session_id)
    if rec is not None and rec.status == "running":
        return {"task_status": "running", "running": True,
                "last_seq": rec.broadcaster.seq, "round_id": rec.round_id, "question": rec.question}
    from app.core.redis import redis_llen

    llen = await redis_llen(f"sse_events:{session_id}")
    return {"task_status": getattr(sess, "task_status", None) or "none", "running": False,
            "last_seq": max(-1, llen - 1), "round_id": None, "question": None}


@router.get("/sessions/{session_id}/context")
async def session_context_get(session_id: str, user: dict = Depends(get_business_user)):
    """上下文水位与压缩状态（2026-09-15）：进度条 + 压缩卡片数据源。"""
    if not _is_uuid(session_id):
        raise app_error("E011", "非法的会话 ID", status_code=422)
    async with get_global_engine().connect() as conn:
        sess = (await conn.execute(select(Session).where(Session.id == session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "会话不存在或无权访问", status_code=404)
    from app.services.context_service import session_context

    return await session_context(session_id)


@router.post("/sessions/{session_id}/compact")
async def session_compact(session_id: str, user: dict = Depends(get_business_user)):
    """手动压缩上下文（2026-09-15）：阻塞式回合——压缩期间该会话不能提问（/ask 拒 409）。"""
    if not _is_uuid(session_id):
        raise app_error("E011", "非法的会话 ID", status_code=422)
    async with get_global_engine().connect() as conn:
        sess = (await conn.execute(select(Session).where(Session.id == session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "会话不存在或无权访问", status_code=404)
    # 压缩与对话互斥：会话有 running 任务时拒绝（另一处压缩持锁同样拒绝）
    if get_task(session_id) is not None or getattr(sess, "task_status", None) == "running":
        raise app_error("E011", "会话正在执行任务，请等完成后再压缩", status_code=409)
    from app.services import context_service

    token = await context_service.acquire_lock(session_id)
    if not token:
        raise app_error("E011", "正在压缩中，请稍候", status_code=409)
    try:
        r = await context_service.compact_session(
            session_id, getattr(sess, "department_id", None), user.get("role"), reason="manual")
    finally:
        await context_service.release_lock(session_id, token)
    if not r.get("ok"):
        raise app_error("E011", str(r.get("error") or "压缩失败"), status_code=400)
    return {"ok": True, **await context_service.session_context(session_id)}


# ===== 二期 M8：图表 PNG 导出 + 文档在线预览 =====

@router.post("/charts/{chart_id}/png")
async def chart_png(chart_id: str, user: dict = Depends(get_business_user)):
    """图表 PNG 导出：按 chart_outputs.id 渲染 option → PNG → 返回 file_path。

    归属校验：chart 所属 session 必须属于当前用户。
    """
    if not _is_uuid(chart_id):  # E-03(API)：非 UUID 直接 422（防 asyncpg DataError → 500）
        raise app_error("E011", "非法的图表 ID", status_code=422)
    from app.services.chart_png import render_option

    async with get_global_engine().connect() as conn:
        row = (
            await conn.execute(
                select(ChartOutput).where(ChartOutput.id == chart_id)
            )
        ).first()
        if row is None:
            raise app_error("E011", "图表不存在", status_code=404)
        sess = (await conn.execute(select(Session).where(Session.id == row.session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "无权访问该图表", status_code=403)
        out_dir = Path(f"{_settings.output_dir}/{row.session_id}/{row.round_id}")
    try:
        png_path = await render_option(row.option_json, str(out_dir))
    except RuntimeError as e:
        raise app_error("E011", sanitize_err_text(str(e)), status_code=400)  # R6：错误脱敏
    # 记录 png_path 供后续（docx 内嵌复用）
    async with get_global_engine().begin() as conn:
        await conn.execute(
            update(ChartOutput).where(ChartOutput.id == chart_id).values(png_path=png_path)
        )
    # N3（红队二次扩展）：file_path 改返文件名（原返服务器绝对路径 /data/outputs/... 信息外泄；
    # 前端 PreviewPanel 只消费 download_url，已核实）
    return {"file_path": Path(png_path).name, "download_url": output_url(row.session_id, row.round_id, Path(png_path).name)}


class PreviewRequest(BaseModel):
    session_id: str
    round_id: int
    file_path: str   # 产出物 URL 或服务器路径


@router.post("/preview")
async def doc_preview(req: PreviewRequest, user: dict = Depends(get_business_user)):
    if not _is_uuid(req.session_id):  # M24
        raise app_error("E011", "非法的会话 ID", status_code=422)
    """文档在线预览：docx→HTML（iframe 可读）/ pptx→PDF（浏览器原生渲染）。

    4.1：转换走 officecli_adapter（OfficeCLI 优先，LibreOffice 回退，mtime 缓存互通）。
    转换产物存回 output 目录，走 outputs_router 下载链路（带 JWT）。
    """
    from app.services.officecli_adapter import to_html, to_pdf

    async with get_global_engine().connect() as conn:
        sess = (await conn.execute(select(Session).where(Session.id == req.session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "无权访问该会话", status_code=403)

    # file_path 两种形态：{api_prefix}/outputs/... URL 或服务器路径
    # S1 修复：仅允许本会话产出目录与本人上传目录内的文件（拒绝绝对路径/跨会话/符号链接逃逸）
    src = req.file_path
    _out_url_prefix = f"{_settings.api_prefix.rstrip('/')}/outputs/"
    if src.startswith(_out_url_prefix):
        from urllib.parse import unquote

        rel = unquote(src[len(_out_url_prefix):])  # 2026-09-15：output_url 已编码（#/?/% 等）
        src = str(Path(_settings.output_dir) / rel)
    src_path = Path(src)
    try:
        resolved_src = src_path.resolve()
    except OSError:
        raise app_error("E011", "文档不存在", status_code=404)
    out_root = (Path(_settings.output_dir) / req.session_id).resolve()
    up_root = (Path(_settings.upload_dir) / "users" / str(user["user_id"]) / req.session_id).resolve()
    if not (resolved_src.is_relative_to(out_root) or resolved_src.is_relative_to(up_root)):
        raise app_error("E006", "无权访问该路径", status_code=403)
    if not resolved_src.exists():
        raise app_error("E011", "文档不存在", status_code=404)
    src_path = resolved_src

    out_dir = Path(f"{_settings.output_dir}/{req.session_id}/{req.round_id}")
    # 2026-09-17（缓存审计 P1）：预览转换缓存提到**会话级**——原缓存只在本轮产出目录里，
    # 换个 ask 再看同一份 pptx/docx 必落空、LibreOffice 再跑 30 秒。转换产物仍落在本轮目录
    # （URL/鉴权链路不变），会话级目录只做"转换结果复用"。
    from app.services.preview_cache import cache_dir_for

    _preview_cache = str(cache_dir_for(out_root))
    try:
        # 2026-08-20（P6）：.pdf 不再 to_html（OfficeCLI 不支持 .pdf → 400，走查实测）——
        # 直接返回原文件供前端 pdfjs canvas 渲染；upload_dir 内文件拷贝到 out_dir
        # （保证走 outputs 下载链路鉴权，与转换产物一致）
        if src_path.suffix.lower() == ".pdf":
            if src_path.is_relative_to(up_root):
                out_dir.mkdir(parents=True, exist_ok=True)
                target = out_dir / src_path.name
                if not target.exists():
                    shutil.copy2(str(src_path), str(target))
                converted = str(target)
            else:
                converted = str(src_path)
        elif src_path.suffix.lower() == ".pptx":
            converted = await to_pdf(str(src_path), str(out_dir), _preview_cache)
        else:
            converted = await to_html(str(src_path), str(out_dir), _preview_cache)
    except RuntimeError as e:
        raise app_error("E011", sanitize_err_text(str(e)), status_code=400)  # R6：错误脱敏
    name = Path(converted).name
    return {"preview_url": output_url(req.session_id, req.round_id, name), "filename": name}


# 产出物下载路由（独立 router，路径保持 /api/v1/outputs/...）
outputs_router = APIRouter(tags=["outputs"])


async def _lookup_output_label(session_id: str, filename: str) -> str | None:
    """从 chat_messages.outputs JSONB 中查找该文件名对应的可读 label（下载名用，如《周报》.docx）。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT outputs FROM chat_messages WHERE session_id=:sid AND role='assistant' AND outputs IS NOT NULL"),
                {"sid": session_id},
            )
        ).all()
    for row in rows:
        outs = row[0]
        if not isinstance(outs, list):
            continue
        for o in outs:
            if isinstance(o, dict) and o.get("file_path", "").endswith("/" + filename) and o.get("label"):
                return str(o["label"])
    return None


@outputs_router.get("/outputs/{session_id}/{round_id}/{filename}")
async def get_output(
    session_id: str,
    round_id: int,
    filename: str,
    user: dict = Depends(get_business_user),
):
    """产出物下载（受保护）：仅会话所属用户可访问。

    替代原公开静态挂载——接口 URL 不再裸奔，前端 fetch 携带 JWT。
    B10：下载名返回可读 label（Content-Disposition RFC 5987 filename*），用户知道下载的是什么报告。
    """
    from urllib.parse import quote

    from fastapi.responses import FileResponse

    from app.core.config import get_settings

    _settings = get_settings()
    # E-02（2026-08-10，🔴 安全）：filename 路径穿越修复——原 Path 拼接无 resolve/is_relative_to
    # 校验，Starlette 对 %2F 做 URL-decode → ../../ 可穿越到任意服务器文件（含 .env → 伪造 JWT 提权）。
    # 对齐 doc_preview 的 containment 校验模式。
    if (
        not filename
        or "/" in filename
        or "\\" in filename
        # 2026-09-15（走查实修）：原为 `".." in filename` **子串**判断——把合法文件名里连续两个点
        # （实测产出物 `…21.08.58..md`：源名截断后以点结尾再拼 .md）也当路径穿越拒了 → 404，
        # 浏览器把错误 JSON 存成"下载"。分隔符已禁（/ 与 \），且下方 resolve + is_relative_to
        # 二次兜底，故按**路径段**判（对齐 zip_utils/skill_file_service 同款写法）
        or filename in (".", "..")
    ):
        raise app_error("E011", "文件不存在或已过期（产出物保留 21 天）", status_code=404)
    async with get_global_engine().connect() as conn:
        sess = (await conn.execute(select(Session).where(Session.id == session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "无权访问该文件", status_code=403)
    try:
        base = (Path(_settings.output_dir) / session_id / str(round_id)).resolve()
        file_path = (base / filename).resolve()
        if not file_path.is_relative_to(base):
            raise app_error("E011", "文件不存在或已过期（产出物保留 21 天）", status_code=404)
    except OSError:
        raise app_error("E011", "文件不存在或已过期（产出物保留 21 天）", status_code=404)
    if not file_path.exists() or not file_path.is_file():
        raise app_error("E011", "文件不存在或已过期（产出物保留 21 天）", status_code=404)
    label = await _lookup_output_label(session_id, filename)
    # 2026-08-10：兜底分支也必须 quote——label 查不到的派生文件（LibreOffice 转换的
    # preview_*.html/PDF）文件名含中文时，原始文件名进 Content-Disposition → Starlette
    # 按 HTTP 规范 latin-1 编码 header 值 → UnicodeEncodeError 500（在线浏览崩，实测复现）
    safe_name = quote(label if label else filename)
    disposition = f'attachment; filename="{safe_name}"; filename*=UTF-8\'\'{safe_name}'
    return FileResponse(file_path, headers={"Content-Disposition": disposition})


async def _maybe_unzip_saved(file_path: Path, size: int, safe_name: str, upload_dir: Path,
                             extra: dict, fname: str) -> None:
    """落盘后的 zip 自动解压（超过 session_zip_auto_unzip_max_mb 只存原件并记 warning——
    解压需整包读入内存，防大 zip 打爆；需要时由 agent 用 zip_extract 按需解压）。"""
    if size > _settings.session_zip_auto_unzip_max_mb * 1024 * 1024:
        extra["warnings"].append(f"{fname}: zip 超过 {_settings.session_zip_auto_unzip_max_mb}MB，未自动解压")
        return
    content = await asyncio.to_thread(file_path.read_bytes)
    await _auto_unzip_session_zip(content, safe_name, upload_dir, extra, fname)


async def _auto_unzip_session_zip(content: bytes, safe_name: str, upload_dir: Path,
                                  extra: dict, fname: str) -> None:
    """会话 zip 上传自动解压到同目录 {stem}_unzip/（2026-08-21；失败不阻断，仅记 warning）。

    2026-09-15：自 upload_files 内联块抽出——分片取件路径复用同一逻辑。
    """
    unzip_dir = upload_dir / f"{Path(safe_name).stem}_unzip"
    try:
        from app.services.zip_utils import safe_extract_zip_filtered

        # 2026-09-17（用户要求）：统一安全锁——可执行/载荷成员 + macOS 元数据垃圾
        # **跳过不落盘**，并回报清单（前端小浮窗「已跳过 N 个文件」+ 解压目录内一份跳过清单.md）
        unzipped, skipped = await asyncio.to_thread(
            safe_extract_zip_filtered, content, unzip_dir,
            _settings.session_zip_max_total_bytes,
            _settings.session_zip_max_files,
            _settings.session_zip_max_depth,
        )
        extra["unzipped_count"] += len(unzipped)
        if skipped:
            from app.services.zip_utils import write_skip_manifest

            await asyncio.to_thread(write_skip_manifest, unzip_dir, skipped)
            extra.setdefault("skipped", []).extend(
                {"zip": fname, "name": n, "rule": r} for _s, n, r in skipped
            )
            logger.info("会话 zip 跳过 %d 个成员（可执行/载荷或 macOS 垃圾）zip=%s", len(skipped), fname)
    except ValueError as e:
        extra["warnings"].append(f"{fname}: {str(e)[:100]}")
        logger.warning("会话 zip 自动解压失败 %s: %s", fname, str(e)[:150])


@router.post("/files")
async def upload_files(
    session_id: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    staged_files: str = Form(default=""),   # 2026-09-15：分片暂存凭据（[{"upload_id","file_name"}]）
    user: dict = Depends(get_business_user),
):
    if not _is_uuid(session_id):  # E-03(API)：非 UUID 直接 422
        raise app_error("E011", "非法的会话 ID", status_code=422)
    engine = get_global_engine()
    async with engine.connect() as conn:
        sess = (await conn.execute(select(Session).where(Session.id == session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "会话不存在或无权访问", status_code=404)

    # 2026-09-15：分片暂存凭据解析（大媒体 >50MB 由前端切片上传再取件，见 services/upload_chunks）
    from app.services import upload_chunks as uc

    staged_desc = uc.parse_staged(staged_files, limit=20)
    if not files and not staged_desc:
        raise app_error("E003", "没有收到文件", status_code=400)
    # F 扩展（红队二次）：上传数量上限——原仅类型/大小限制，可单请求批量传大量小文件打爆磁盘
    if len(files) + len(staged_desc) > 20:
        raise app_error("E003", f"单次最多上传 20 个文件（收到 {len(files) + len(staged_desc)} 个）", status_code=400)
    async with engine.begin() as conn:
        cnt = (await conn.execute(text("SELECT COUNT(*) FROM chat_files WHERE session_id = :s"),
                                  {"s": session_id})).scalar() or 0
        if cnt + len(files) + len(staged_desc) > 100:
            raise app_error("E003", f"会话文件数已达上限（100 个，当前 {cnt} 个）——请先删除不需要的文件",
                            status_code=400)

    saved = []
    extra: dict = {"unzipped_count": 0, "warnings": []}
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in ALLOWED_TYPES:
            raise app_error("E003", f"不支持的文件类型 {ext or '未知'}", status_code=400)
        file_id = str(uuid.uuid4())
        # 用户级隔离（四期：/data/uploads/users/{user_id}/{session_id}/，防跨用户读取）
        upload_dir = Path(f"{_settings.upload_dir}/users/{user['user_id']}/{session_id}")
        upload_dir.mkdir(parents=True, exist_ok=True)
        # 2026-08-07：落盘保留可读原始文件名（唯一前缀 + sanitize 原名）——agent 的 file_path
        # 直接可见原始文件名而非 UUID 乱码；sanitize_filename 防路径注入
        safe_name = sanitize_filename(f.filename or "unnamed")
        # I38（2026-08-14）：超长文件名落盘超 NAME_MAX（255 字节）→ OSError 500。
        # 按 UTF-8 字节截断（CJK 3 字节/字），留 uuid8 前缀余量
        safe_name = safe_name.encode("utf-8")[:240].decode("utf-8", errors="ignore").strip() or "file"
        file_path = upload_dir / f"{file_id[:8]}_{safe_name}"
        # 2026-09-15：**全部类型流式落盘**（音视频 600MB / 文档 200MB，不整读进内存）——
        # 原文档类走 read_limited 整读进内存（20MB 上限时代的写法，放宽上限后必须改流式）
        is_media = ext in MEDIA_TYPES
        # 2026-09-16：上限改为管理端动态配置（config_service.upload_limits）
        _lim = await get_upload_limits()
        limit_mb = _lim["media_mb"] if is_media else _lim["session_doc_mb"]
        try:
            size, head = await save_limited(f, file_path, limit_mb * 1024 * 1024)
        except ValueError as e:
            raise app_error("E003", str(e), status_code=400)
        # SEC-16：头字节嗅探（伪造扩展名文件拒绝——改名 exe/任意文本冒充 Office/PDF/图片/视频；
        # 32 字节头足够全部魔数判定，ftyp 特判需前 12 字节）
        magic_err = check_magic_bytes(head, ext)
        if magic_err:
            file_path.unlink(missing_ok=True)  # 已落盘——嗅探失败即清理，防孤儿文件
            raise app_error("E003", f"{magic_err}: {f.filename}", status_code=400)

        try:
            async with engine.begin() as conn:
                await conn.execute(
                    insert(ChatFile).values(
                        id=file_id,
                        session_id=session_id,
                        # I38 修复（2026-08-14）：原名超长仍会 500——DB file_name 同步截断
                        # （file_path 已截断到 240 字节，DB 列同样 255 字节上限）
                        file_name=(f.filename or "unnamed").encode("utf-8")[:240].decode("utf-8", errors="ignore") or "unnamed",
                        file_path=str(file_path),
                        file_size=size,
                        file_type=ext.lstrip("."),
                    )
                )
        except Exception:
            # D30（2026-08-10）：DB insert 失败清理已写物理文件（原写盘与 DB 分离，失败即孤儿）
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        saved.append({"file_id": file_id, "file_name": f.filename, "size": size})
        # 2026-08-21：zip 上传自动解压到同目录 {stem}_unzip/ 子文件夹（会话级；
        # agent 经 skill_router 注入的解压清单自行检查调用）。解压失败不阻断上传——
        # zip 原件已存（file_parse 可读清单、zip_extract 可二次解压），仅记 warning
        if ext == ".zip":
            await _maybe_unzip_saved(file_path, size, safe_name, upload_dir, extra, f.filename or "")

    # 2026-09-15（大媒体分片）：取件 → 校验（类型/大小/魔数）→ 移入会话上传目录 → 落库
    for desc in staged_desc:
        up_id = str(desc.get("upload_id") or "")
        orig_name, src = uc.take_staged(user, desc)
        ext = Path(orig_name).suffix.lower()
        if ext not in ALLOWED_TYPES:
            uc.drop_session(up_id)
            raise app_error("E003", f"不支持的文件类型 {ext or '未知'}", status_code=400)
        is_media = ext in MEDIA_TYPES
        # 2026-09-16：上限改为管理端动态配置（config_service.upload_limits）
        _lim = await get_upload_limits()
        limit_mb = _lim["media_mb"] if is_media else _lim["session_doc_mb"]
        size = await asyncio.to_thread(lambda p=src: p.stat().st_size)
        if size > limit_mb * 1024 * 1024:
            uc.drop_session(up_id)
            raise app_error("E003", f"文件超过 {limit_mb}MB 限制: {orig_name}", status_code=400)
        head = await asyncio.to_thread(lambda p=src: p.open("rb").read(32))
        magic_err = check_magic_bytes(head, ext)
        if magic_err:
            uc.drop_session(up_id)
            raise app_error("E003", f"{magic_err}: {orig_name}", status_code=400)
        file_id = str(uuid.uuid4())
        upload_dir = Path(f"{_settings.upload_dir}/users/{user['user_id']}/{session_id}")
        upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_filename(orig_name, "media")
        safe_name = safe_name.encode("utf-8")[:240].decode("utf-8", errors="ignore").strip() or "file"
        file_path = upload_dir / f"{file_id[:8]}_{safe_name}"
        await asyncio.to_thread(shutil.move, str(src), str(file_path))  # 同盘移动（秒级，不过内存）
        uc.drop_session(up_id)  # 取走即清暂存目录
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    insert(ChatFile).values(
                        id=file_id,
                        session_id=session_id,
                        file_name=orig_name.encode("utf-8")[:240].decode("utf-8", errors="ignore") or "unnamed",
                        file_path=str(file_path),
                        file_size=size,
                        file_type=ext.lstrip("."),
                    )
                )
        except Exception:
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        saved.append({"file_id": file_id, "file_name": orig_name, "size": size})
        if ext == ".zip":
            await _maybe_unzip_saved(file_path, size, safe_name, upload_dir, extra, orig_name)

    return {"files": saved, "extra": extra}


@router.get("/sessions/{session_id}/files")
async def list_session_files(session_id: str, user: dict = Depends(get_business_user)):
    """会话文件列表（浮窗展示用；删除接口 DELETE /files/{id} 配套）。"""
    if not _is_uuid(session_id):  # E-03(API)
        raise app_error("E011", "非法的会话 ID", status_code=422)
    engine = get_global_engine()
    async with engine.connect() as conn:
        sess = (await conn.execute(select(Session).where(Session.id == session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "会话不存在或无权访问", status_code=404)
        rows = (
            await conn.execute(
                select(ChatFile).where(ChatFile.session_id == session_id).order_by(ChatFile.created_at)
            )
        ).all()
    return {
        "files": [
            {
                "file_id": r.id,
                "file_name": r.file_name,
                "file_size": r.file_size,
                "file_type": r.file_type,
            }
            for r in rows
        ]
    }


@router.delete("/files/{file_id}")
async def delete_session_file(file_id: str, user: dict = Depends(get_business_user)):
    """删除会话文件：归属校验（文件所属会话属于当前用户）→ 删物理文件 → 删 DB 记录。"""
    if not _is_uuid(file_id):  # E-03(API)
        raise app_error("E011", "非法的文件 ID", status_code=422)
    engine = get_global_engine()
    # begin()：删除必须提交（connect() 默认不自动提交，DELETE 会被回滚）
    async with engine.begin() as conn:
        row = (await conn.execute(select(ChatFile).where(ChatFile.id == file_id))).first()
        if row is None:
            raise app_error("E011", "文件不存在", status_code=404)  # M2：统一"文件不存在"语义（与 :350 一致）
        sess = (await conn.execute(select(Session).where(Session.id == row.session_id))).first()
        if sess is None or sess.user_id != user["user_id"]:
            raise app_error("E006", "会话不存在或无权访问", status_code=404)
        # 物理删除（仅删除该会话目录下的文件；目录由会话 21 天清理统一回收）
        try:
            Path(row.file_path).unlink(missing_ok=True)
        except OSError:
            pass
        await conn.execute(delete(ChatFile).where(ChatFile.id == file_id))
    return {"deleted": file_id}
