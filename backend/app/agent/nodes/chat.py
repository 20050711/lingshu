"""chat 节点：收尾答复（confirm 拒绝/超时/轮次超限后，无工具调用）。

2026-09-08 重构：强制收尾不再调用 LLM——系统直接生成两分类平台说明
（break_notice.build_break_notice），原因：
- 原 chat LLM 收尾输出常含工具调用标记 → 被拦截 → 用户看到无信息的
  "任务已完成，请查看上方产出与工具执行情况。"（走查 9-4 会话实况）；
- 收尾轮带完整历史+无工具 schema → 单次 ~31K 输入全价重算（缓存 miss 大头）；
平台说明模板让"中断何时发生、卡在哪、建议什么"对用户可见，并写续跑标记
（"回复「继续」从断点接着做"）。
"""
from __future__ import annotations

import asyncio

from langchain_core.runnables import RunnableConfig

from app.agent.state import AgentState
from app.core.logging import get_logger

logger = get_logger("agent.node.chat")


def _prune_unpaired_tools(messages: list[dict]) -> list[dict]:
    """移除未配对的 tool_calls/tool 消息（DeepSeek 校验要求成对出现）。

    - assistant(tool_calls) 若后续没有对应 tool 响应（confirm 拒绝/超时）→ 删除
    - 孤儿 tool 消息（其 tool_call_id 无对应的 assistant(tool_calls)）→ 删除
    """
    result: list[dict] = []
    pending_ids: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            pending_ids.update(tc.get("id", "") for tc in m["tool_calls"])
            result.append(m)
        elif m.get("role") == "tool":
            if m.get("tool_call_id") in pending_ids:
                pending_ids.discard(m.get("tool_call_id"))
                result.append(m)
            # 孤儿 tool 消息丢弃
        else:
            result.append(m)
    # 移除末尾未配对的 assistant(tool_calls)
    while result and result[-1].get("role") == "assistant" and result[-1].get("tool_calls"):
        result.pop()
    return result


async def run_chat(state: AgentState, config: RunnableConfig) -> dict:
    c = config["configurable"]
    events: asyncio.Queue = c["events"]

    messages: list[dict] = list(state.get("messages") or [])
    messages = _prune_unpaired_tools(messages)

    from app.agent.nodes.break_notice import is_platform_notice
    from app.agent.nodes.break_notice import PLATFORM_TAG, build_break_notice, mark_interrupted

    # 2026-09-08：平台直发两分类说明（不再调 LLM）。S9/B5 的用户问题仅为历史落库完整性保留。
    # 例外：plan_router/plan_approval 直进 chat 的注记本就是用户可读文本（计划批准超时/
    # 修订上限/解析失败）——原样输出，不被模板覆盖（模板内容面向"工具轮失败"场景）
    outputs_n = len(state.get("round_outputs") or [])
    converge_note = str(state.get("converge_note") or "")
    plan_note = converge_note and ("计划" in converge_note[:40] or "【收尾提示】" in converge_note)
    if plan_note:
        content = converge_note if is_platform_notice(converge_note) else f"{PLATFORM_TAG}{converge_note}"
        logger.info("node=chat 计划类注记收尾 note='%s'", converge_note[:40])
    else:
        content = build_break_notice(state)
        logger.info("node=chat 平台说明收尾 outputs=%d content_len=%d", outputs_n, len(content))
    # 续跑标记（仅工具轮失败场景；计划类超时走"重开卡"语义，不标记中断）：
    # 下轮 ask 的 _load_history 注入"上轮被中断+产出清单"引导，用户回「继续」即可接续
    if not plan_note:
        try:
            reason = ""
            for e in reversed(state.get("tool_events") or []):
                if isinstance(e, dict) and e.get("status") == "error":
                    reason = str(e.get("detail") or "")[:100]
                    break
            reason = reason or "连续多次尝试未取得进展"
        except Exception:
            reason = ""
        mark_interrupted(str(state.get("session_id", "")), int(state.get("round_id") or 0), reason, outputs_n,
                         asked_n=int(state.get("asked_this_round") or 0))

    chunk_size = 16  # P4（2026-08-10）：打字机节流（原 4 字符/15ms=66 次 setState/s，前端渲染+滚动卡顿）
    for i in range(0, len(content), chunk_size):
        await events.put({"event": "text", "delta": content[i : i + chunk_size]})
        if i + chunk_size < len(content):
            await asyncio.sleep(0.025)
    return {"messages": [*messages, {"role": "assistant", "content": content}]}
