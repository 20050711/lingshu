"""事件广播器（2026-08-19 架构 P0 下沉）：graph 事件 → guard_output 过滤 → Redis
持久化（带 seq）+ 内存广播。

从 chat_service 下沉：打破 task_registry ↔ chat_service 循环依赖（EventBroadcaster
被 task_registry 引用）。**零依赖 chat_service/task_registry**——guard_output/
contains_tool_markup/redis_* 均为延迟 import（全部只触 core 层，无环）。

使用方：
- chat_service.start_bg_task 构造 + relay/tail_stream/replay_stream 消费（bc.put/subscribe）
- task_registry.TaskRecord 类型引用
"""
from __future__ import annotations

import asyncio
import json
import uuid

from app.core.logging import get_logger

logger = get_logger("services.events")


class EventBroadcaster:
    """graph 事件 → guard_output 过滤 → Redis 持久化（带 seq）+ 内存广播。

    2026-08-18（任务后台化）：put 仅由 relay 调用（单一消费者——guard_output 的
    text 64/8 重叠窗口需要连续缓冲状态）；转发器 subscribe 后从自己的队列消费。
    seq = Redis list index；text/heartbeat 不持久化（text 是瞬态 token 流，
    历史文本由 DB 承载，重连期间无文本用前端 ThinkTag 兜底）。
    过滤先于持久化——重连回放不泄露 .env 密钥。
    """

    PERSISTED = {"mode", "tool", "intent", "result", "chart", "plan", "question",
                 "progress", "done", "error", "aborted"}
    TEXT_WINDOW = 64   # 过滤窗口长度（须 ≥ 最长敏感值：deepseek key 35 字符，sk- 形态 13+；≤64 全部覆盖）
    TEXT_KEEP = 8      # 窗口间重叠字符数：≥8 字符敏感值跨边界时必被某窗口完整包含并整体替换

    def __init__(self, session_id: str, ttl: int):
        self.session_id = session_id
        self.ttl = ttl
        self.queue: asyncio.Queue = asyncio.Queue()   # graph 写入的原队列（run_agent 仍用它）
        self.subs: dict[str, asyncio.Queue] = {}      # 转发器订阅队列（qid → queue）
        # seq = Redis 事件流（sse_events:{session}）的会话级累积 index——跨轮连续
        # （事件流按会话 TTL 1800 保留，同会话新一轮的事件接在旧轮末尾之后）。
        # start_seq：本任务启动时流末尾（首连/回放只取本任务自身事件，不混入旧轮）
        self.start_seq = -1
        self.seq = -1
        self.charts_seen: list[dict] = []             # 已发 chart 事件（降级落库保 PNG 可导出）
        self.text_pending = ""                        # 未发出 text 缓冲（降级落库的 assistant 文本）
        self.leaked_any = False
        self._text_buf = ""
        self._xml_leak = False                        # 2026-08-19：text 流检出工具标记 → 后续全作废

    def subscribe(self) -> str:
        qid = uuid.uuid4().hex[:8]
        self.subs[qid] = asyncio.Queue()
        return qid

    def unsubscribe(self, qid: str) -> None:
        self.subs.pop(qid, None)

    def _emit(self, frame: dict) -> None:
        for q in list(self.subs.values()):
            q.put_nowait(frame)

    async def _persist_frame(self, d: dict) -> None:
        """写 Redis 事件流（含 event/seq 键）+ 滑动 TTL。失败静默（Redis 降级不影响转发）。

        2026-08-19（S1-2）：静默 except:pass 曾致 plan 事件写流失败（广播仍发出，
        断连/重连场景事件永久丢失）——加 WARN 日志 + 失败重试一次（仅重试关键帧）。
        """
        from app.core.redis import redis_expire, redis_rpush

        key = f"sse_events:{self.session_id}"
        try:
            # 2026-08-19（S1-2）：await 加超时——Redis 半开时 rpush 曾永久挂起拖死 relay
            # 单消费者（plan/question 事件丢失）；5s 超时快速失败走重试/告警路径
            await asyncio.wait_for(redis_rpush(key, json.dumps(d, ensure_ascii=False)), timeout=5)
            await asyncio.wait_for(redis_expire(key, self.ttl), timeout=5)
        except Exception as e:
            logger.warning("事件流写入失败（重试一次）session=%s event=%s seq=%s err=%s",
                           str(self.session_id)[:8], d.get("event"), d.get("seq"), str(e)[:120])
            try:
                await asyncio.wait_for(redis_rpush(key, json.dumps(d, ensure_ascii=False)), timeout=5)
                await asyncio.wait_for(redis_expire(key, self.ttl), timeout=5)
            except Exception as e2:
                logger.error("事件流写入重试仍失败 session=%s event=%s seq=%s err=%s（广播不受影响，"
                             "断连重连将丢失该帧）", str(self.session_id)[:8], d.get("event"),
                             d.get("seq"), str(e2)[:120])

    async def _emit_text(self, delta: str) -> None:
        """text 增量：持久化（带 seq——重连续播"回复到一半"的文本）+ 广播。"""
        self.seq += 1
        d = {"event": "text", "delta": delta, "seq": self.seq}
        await self._persist_frame(d)
        self._emit(d)

    async def put(self, item: dict) -> None:
        """仅 relay 调用。item = {"event": type, ...payload}。"""
        from app.services.output_guard import guard_output

        event = item["event"]
        payload = {k: v for k, v in item.items() if k != "event"}
        if event == "chart":
            self.charts_seen.append(payload)
        if event == "text":
            delta = str(payload.get("delta") or "")
            if not delta:
                return
            self._text_buf += delta
            while len(self._text_buf) >= self.TEXT_WINDOW:
                window = self._text_buf[:self.TEXT_WINDOW]
                emit_len = self.TEXT_WINDOW - self.TEXT_KEEP
                filtered_all, hits = guard_output(window)
                if hits:
                    self.leaked_any = True
                # 2026-08-19（S1-2 纵深）：所有 text 帧必经此处的**最终防线**——节点级
                # 拦截（chat/verify/plan 分支）之外再兜底一层：窗口含工具调用标记
                # （<tool_calls/<invoke/<function=/<parameter）→ **整段作废 + 置位**：
                # 幻觉 XML 一旦开始剩余基本都是 XML，用 _xml_leak 标志把后续全部替换
                # 为占位（不靠窗口滑动——TEXT_KEEP=8 重叠会截断 11 字符标记残留泄漏）。
                from app.core.text_utils import contains_tool_markup

                if self._xml_leak or contains_tool_markup(filtered_all):
                    self._xml_leak = True
                    self._text_buf = ""
                    await self._emit_text("（检测到疑似工具调用内容，已自动屏蔽）")
                    return
                await self._emit_text(filtered_all[:emit_len])
                self._text_buf = window[-self.TEXT_KEEP:] + self._text_buf[self.TEXT_WINDOW:]
            return
        if event == "heartbeat":
            return
        raw = json.dumps(payload, ensure_ascii=False)
        filtered_raw, hits = guard_output(raw)
        if hits:
            self.leaked_any = True
        if event in self.PERSISTED:
            self.seq += 1
            d = json.loads(filtered_raw)
            d["seq"] = self.seq
            d["event"] = event  # 事件名入流（payload 已剥离 event 键；回放/转发器按此还原帧）
            await self._persist_frame(d)
            self._emit({"event": event, **d})
        else:
            self._emit({"event": event, **json.loads(filtered_raw)})

    async def flush_text(self) -> None:
        """流末尾 flush 剩余 text 缓冲 + 敏感值屏蔽提示（relay 收 END_MARKER 后调用）。"""
        from app.core.text_utils import contains_tool_markup
        from app.services.output_guard import guard_output

        if self._text_buf:
            # 2026-08-19（S1-2）：_xml_leak 已置位（put 窗口检出过工具标记、占位已发）
            # → 尾部残留（可能截断的标记尾巴）直接丢弃，不再泄漏
            if self._xml_leak:
                self._text_buf = ""
                return
            filtered_tail, hits = guard_output(self._text_buf)
            if hits:
                self.leaked_any = True
            # 2026-08-19（S1-2）：尾部缓冲含工具调用标记 → 整段作废（与 put 窗口同口径）
            if contains_tool_markup(filtered_tail):
                filtered_tail = "（检测到疑似工具调用内容，已自动屏蔽）"
                self.leaked_any = True
            self.text_pending = filtered_tail
            await self._emit_text(filtered_tail)
            self._text_buf = ""
        if self.leaked_any:
            self._emit({"event": "text",
                        "delta": "\n\n> 检测到回答中出现疑似密钥信息，已自动屏蔽（如确需查看请检查配置）。"})


def _frame_to_sse(frame: dict) -> str:
    """帧 dict → SSE 文本（event 键单独取行首）。"""
    return f"event: {frame['event']}\ndata: {json.dumps({k: v for k, v in frame.items() if k != 'event'}, ensure_ascii=False)}\n\n"
