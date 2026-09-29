"""会话上下文压缩服务（2026-09-15：上下文进度条 + 手动/被动压缩）。

职责：
- **会话级结构化压缩摘要**（≤ boundary 轮由摘要代表，替代原文注入）——手动按钮 / 水位触发 /
  空闲扫描三个入口共用同一个引擎（统一的 compact 语义）
- **水位统计**（ctxstat）：上次 ask 的上下文分解（base=system+动态上下文+问题，history=历史注入），
  进度条数据源；`GET /chat/sessions/{id}/context`

存储（均 Redis，TTL 与会话保留 21 天对齐）：
- `ctxsum:v1:{session_id}` → {"boundary": N, "summary": "...", "created_at": ts}
- `ctxstat:v1:{session_id}` → {"base", "history", "watermark", "budget", "boundary", "updated_at"}
- `ctxlock:v1:{session_id}` → 压缩互斥锁（存在 = 正在压缩；/ask 据此拒绝新提问）

压缩语义：boundary = 最新轮 - keep_rounds（保留最近 N 轮原文）；输入 = 上次摘要 +
(上次 boundary, 新 boundary] 轮次轻量表征；只改"发给模型的历史注入"，库中原文不动；失败不改动状态。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis import get_redis, redis_get, redis_set

logger = get_logger("services.context")
_settings = get_settings()

_SUM_TTL = _settings.history_summary_cache_ttl  # 21 天（与会话保留一致）

# 用户消息里注入的上下文块（【上下文开始】…【上下文结束】）不参与摘要——前端 stripCtx 同源
_CTX_RE = re.compile(r"【上下文开始】.*?【上下文结束】", re.S)


def _sum_key(session_id: str) -> str:
    return f"ctxsum:v1:{session_id}"


def _stat_key(session_id: str) -> str:
    return f"ctxstat:v1:{session_id}"


def _lock_key(session_id: str) -> str:
    return f"ctxlock:v1:{session_id}"


def _strip_ctx(text: str) -> str:
    return _CTX_RE.sub("", text or "").strip()


def _tool_lines(tool_events, limit: int = 3) -> list[str]:
    """工具事件一行式摘要（与 chat_service._tool_event_lines 同语义的本地最小版——避免循环导入）。"""
    out: list[str] = []
    for e in tool_events if isinstance(tool_events, list) else []:
        if not isinstance(e, dict) or e.get("kind") in ("intent", "result"):
            continue
        out.append(f"- {e.get('tool_name', '')}({e.get('status', '')}): {str(e.get('summary') or e.get('brief') or '')[:200]}")
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 读写

async def get_summary(session_id: str) -> dict | None:
    """会话压缩摘要 {"boundary","summary"}；无/损坏返回 None。"""
    try:
        raw = await redis_get(_sum_key(session_id))
        if raw:
            d = json.loads(raw)
            if d.get("summary") and int(d.get("boundary") or 0) > 0:
                return d
    except Exception:
        pass
    return None


async def get_stat(session_id: str) -> dict:
    """水位统计（无记录返回空 dict）。"""
    try:
        raw = await redis_get(_stat_key(session_id))
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


async def save_stat(session_id: str, probe: dict) -> None:
    """ask 结束后落水位：probe = {"history","total","real"}（agent_llm 首轮记录）。"""
    try:
        base = max(int(probe.get("total") or 0) - int(probe.get("history") or 0), 0)
        watermark = int(probe.get("real") or probe.get("total") or 0)
        summ = await get_summary(session_id) or {}
        payload = {
            "base": base,
            "history": int(probe.get("history") or 0),
            "watermark": watermark,
            "budget": _settings.context_budget_tokens,
            "boundary": int(summ.get("boundary") or 0),
            "updated_at": time.time(),
        }
        await redis_set(_stat_key(session_id), json.dumps(payload, ensure_ascii=False), _SUM_TTL)
    except Exception as e:
        logger.warning("水位统计写入失败 session=%s err=%s", str(session_id)[:8], str(e)[:100])


# ---------------------------------------------------------------- 互斥锁

async def acquire_lock(session_id: str) -> str | None:
    """获取压缩互斥锁；返回持有 token（释放时校验）。None=已被他人持有。

    2026-09-15（自冲突审计）：锁值改为随机 token + Lua 校验释放——原实现无条件 DEL，
    TTL 过期后他人重取锁的窗口内，先前的持有者释放会误删新持有者的锁。
    """
    token = uuid.uuid4().hex
    try:
        r = get_redis()
        if r is None:
            return token
        ok = await r.set(_lock_key(session_id), token, nx=True, ex=_settings.context_compact_lock_ttl)
        return token if ok else None
    except Exception:
        return token  # Redis 故障不阻塞压缩（单进程下并发概率低）


_RELEASE_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] "
                "then return redis.call('del', KEYS[1]) else return 0 end")


async def release_lock(session_id: str, token: str | None) -> None:
    """释放锁（仅当 token 匹配——TTL 过期后被他人重取时不误删）。"""
    if not token:
        return
    try:
        r = get_redis()
        if r is not None:
            await r.eval(_RELEASE_LUA, 1, _lock_key(session_id), token)
    except Exception:
        pass


async def compacting(session_id: str) -> bool:
    """该会话是否正在压缩（/ask 据此拒绝新提问——"压缩期间不能对话"）。"""
    try:
        r = get_redis()
        return bool(r is not None and await r.exists(_lock_key(session_id)))
    except Exception:
        return False


# ---------------------------------------------------------------- 数据装配

async def latest_round(session_id: str) -> int:
    from sqlalchemy import func, select

    from app.core.database import get_global_engine
    from app.models import ChatMessage

    engine = get_global_engine()
    async with engine.connect() as conn:
        v = (await conn.execute(
            select(func.max(ChatMessage.round_id)).where(ChatMessage.session_id == session_id)
        )).scalar()
    return int(v or 0)


async def _round_blocks(session_id: str, after: int, upto: int, max_chars: int,
                        full: bool = False) -> list[str]:
    """(after, upto] 轮次的轻量表征（旧→新）：用户问题 + 答复 + 工具行。

    full=False 时按 500/2000/800 截断（摘要输入用，省 token）；full=True 保留全文（水位估算用）。
    """
    from sqlalchemy import select

    from app.core.database import get_global_engine
    from app.models import ChatMessage

    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(
            select(ChatMessage.role, ChatMessage.content, ChatMessage.tool_events, ChatMessage.round_id)
            .where(ChatMessage.session_id == session_id,
                   ChatMessage.round_id > after, ChatMessage.round_id <= upto)
            .order_by(ChatMessage.created_at)
        )).all()
    by_rid: dict[int, list] = {}
    for role, content, tool_events, rid in rows:
        by_rid.setdefault(int(rid or 0), []).append((role, content, tool_events))
    blocks: list[str] = []
    total = 0
    for rid in sorted(by_rid.keys(), reverse=True):  # 新→旧（超预算丢更早的）
        entry = [f"[第 {rid} 轮]"]
        for role, content, tool_events in by_rid[rid]:
            text = _strip_ctx(str(content or ""))
            if not text:
                continue
            if role == "user":
                entry.append(f"用户：{text if full else text[:500]}")
            elif role == "assistant":
                entry.append(f"答复：{text if full else text[:2000]}")
                lines = _tool_lines(tool_events)
                if lines:
                    entry.append("工具：" + "；".join(lines)[:800])
        s = "\n".join(entry)
        if blocks and total + len(s) > max_chars:
            break
        blocks.append(s)
        total += len(s)
    blocks.reverse()  # 旧→新
    return blocks


# ---------------------------------------------------------------- 摘要引擎

_COMPACT_PROMPT = """你是会话压缩助手。请把下面的会话记录压缩成一份**结构化摘要**，供助手在后续轮次继续服务该用户。
只输出摘要正文（Markdown），不要任何额外说明或前后缀。严格按以下结构：

## 目标与背景
（用户在做什么、口径与约束）
## 已完成
（已完成的事项与关键结论）
## 关键数据
（只保留结论性数字与口径，不要抄原文）
## 未决 / 待办
（问过没答的、明确搁置的）
## 参考物（路径）
（产出文件与读过的文件的 file_path 清单——原样保留路径便于后续 read_output 读回；没有写"无"）

要求：忠实、不编造；保留关键数字与文件名；总长不超过 800 字。"""


async def _summarize(user_input: str, dept_id: str | None, user_role: str | None) -> str:
    from app.agent.llm_client import create_text_client
    from app.services.config_service import get_aux_model_cfg_for_task

    cfg = await get_aux_model_cfg_for_task(dept_id, user_role, "history_summary")
    client, model, tkw = create_text_client(cfg)
    resp = await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": _COMPACT_PROMPT},
                      {"role": "user", "content": user_input}],
            max_tokens=_settings.context_compact_max_tokens,
            **tkw,
        ),
        timeout=_settings.history_summary_timeout,
    )
    return (resp.choices[0].message.content or "").strip()


# ---------------------------------------------------------------- 主入口

async def compact_session(session_id: str, dept_id: str | None = None, user_role: str | None = None,
                          keep_rounds: int | None = None, reason: str = "manual") -> dict:
    """压缩会话上下文（幂等：无可压缩内容时返回 ok=False 且不改状态）。

    **调用方必须先持有会话锁**（acquire_lock；当前三个入口：手动接口 / 提问前水位触发 /
    空闲扫描，均 acquire→compact→release）——本函数自身不做互斥。
    返回 {"ok": bool, "boundary", "summary", "watermark_tokens", "error"}。
    """
    keep = int(keep_rounds if keep_rounds is not None else _settings.context_compact_keep_rounds)
    latest = await latest_round(session_id)
    boundary = latest - keep
    prev = await get_summary(session_id) or {}
    prev_boundary = int(prev.get("boundary") or 0)
    if boundary <= prev_boundary:
        return {"ok": False, "error": f"没有可压缩的内容（已压缩至第 {prev_boundary or 0} 轮）"}

    blocks = await _round_blocks(session_id, prev_boundary, boundary,
                                 _settings.context_compact_input_max_chars)
    if not blocks:
        return {"ok": False, "error": "没有可压缩的内容"}

    user_input = ""
    if prev.get("summary"):
        user_input += f"【上一次压缩摘要（覆盖第 1-{prev_boundary} 轮）】\n{prev['summary']}\n\n"
    user_input += "【本次新增轮次记录（旧→新）】\n" + "\n\n".join(blocks)
    try:
        summary = await _summarize(user_input, dept_id, user_role)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"压缩失败：{str(e)[:150]}"}
    if not summary:
        return {"ok": False, "error": "压缩失败（模型返回为空）"}

    await redis_set(_sum_key(session_id),
                    json.dumps({"boundary": boundary, "summary": summary,
                                "created_at": time.time(), "reason": reason}, ensure_ascii=False),
                    _SUM_TTL)

    # 水位重算：base（上次 ask 的非历史部分）+ 新历史（摘要 + boundary 后保留轮次）
    stat = await get_stat(session_id)
    new_hist = await _estimate_history_tokens(session_id, summary, boundary)
    base = int(stat.get("base") or 0)
    new_wm = base + new_hist
    old_wm = int(stat.get("watermark") or 0)
    try:
        await redis_set(_stat_key(session_id), json.dumps({
            "base": base, "history": new_hist, "watermark": new_wm,
            "budget": _settings.context_budget_tokens, "boundary": boundary,
            "updated_at": time.time(),
        }, ensure_ascii=False), _SUM_TTL)
    except Exception:
        pass
    logger.info("会话压缩完成 session=%s 原因=%s boundary=%d 摘要 %d 字 水位 %d→%d",
                str(session_id)[:8], reason, boundary, len(summary), old_wm, new_wm)
    return {"ok": True, "boundary": boundary, "summary": summary, "watermark_tokens": new_wm}


async def _estimate_history_tokens(session_id: str, summary: str, boundary: int) -> int:
    """压缩后历史注入的 token 估算（摘要块 + boundary 后保留轮次全文；与 _load_history 近似）。"""
    from app.services.token_counter import count_tokens

    total = count_tokens(summary) + 64  # 摘要标题行余量
    try:
        blocks = await _round_blocks(session_id, boundary, 10 ** 9, 10 ** 9, full=True)
        total += count_tokens("\n".join(blocks))
    except Exception:
        pass
    return total


async def session_context(session_id: str) -> dict:
    """进度条/压缩卡片的聚合数据（GET /chat/sessions/{id}/context）。"""
    stat = await get_stat(session_id)
    summ = await get_summary(session_id) or {}
    latest = await latest_round(session_id)
    boundary = int(summ.get("boundary") or 0)
    budget = _settings.context_budget_tokens
    wm = int(stat.get("watermark") or 0)
    return {
        "watermark_tokens": wm,
        "budget_tokens": budget,
        "pct": round(wm * 100 / budget, 1) if budget else 0.0,
        "boundary": boundary or None,
        "summary": summ.get("summary"),
        "rounds": latest,
        "keep_recent": _settings.context_compact_keep_rounds,
        "compactible": latest - _settings.context_compact_keep_rounds > boundary,
        "compacting": await compacting(session_id),
    }


# ---------------------------------------------------------------- 空闲扫描（调度器每小时）

async def compact_idle_sessions(limit: int = 3) -> int:
    """空闲会话被动压缩：空闲 ≥ context_compact_idle_hours、有可压缩内容、水位 ≥ 内容下限。

    每次最多压缩 limit 个（限定 LLM 成本）；单个失败只记日志不阻断。
    """
    from sqlalchemy import text

    from app.core.database import get_global_engine

    engine = get_global_engine()
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT s.id::text AS sid, s.department_id AS dept, u.role AS role "
                "FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.last_activity_at < NOW() - make_interval(hours => :h) "
                "  AND s.last_activity_at > NOW() - make_interval(days => 7) "
                "  AND COALESCE(s.task_status, '') <> 'running' "
                "ORDER BY s.last_activity_at DESC LIMIT 20"),
                {"h": _settings.context_compact_idle_hours})).all()
    except Exception as e:
        logger.warning("空闲压缩扫描查询失败: %s", str(e)[:120])
        return 0

    done = 0
    for row in rows:
        if done >= limit:
            break
        sid = str(row.sid)
        stat = await get_stat(sid)
        if int(stat.get("watermark") or 0) < _settings.context_compact_idle_min_tokens:
            continue
        token = await acquire_lock(sid)
        if not token:
            continue
        try:
            r = await compact_session(sid, row.dept, row.role, reason="idle")
            if r.get("ok"):
                done += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("空闲压缩失败 session=%s err=%s", sid[:8], str(e)[:120])
        finally:
            await release_lock(sid, token)
    return done
