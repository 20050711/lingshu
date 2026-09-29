"""tool_exec 节点：执行 pending_tool_calls 中的工具（React 循环核心）。

流程：白名单二次校验 → QueueManager 排队执行 → tool 结果消息回填
→ 收集产出物（chart/文件）→ 条件边回 agent_llm（继续循环）或 chat（收尾）。

连续 2 个工具失败 → 终止循环。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from langchain_core.runnables import RunnableConfig

from app.agent.queue.queue_manager import get_queue_manager
from app.agent.state import AgentState
from app.agent.tools import ToolContext, get_queue_for, get_tool, queue_label, tool_label
from app.core.config import get_settings
from app.core.json_safe import json_default
from app.core.logging import get_logger

logger = get_logger("agent.tool_exec")
_settings = get_settings()


def _infer_output_type(file_path: str) -> str:
    """2026-08-10：按文件扩展名推断产出类型（deliver 图片 → image 前端 img 直显，不走文档转换）。"""
    name = str(file_path).lower()
    if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")):
        return "image"
    if name.endswith((".mp4", ".webm", ".mov", ".m4v")):
        return "video"
    return "file"


def _script_intent(code: str) -> str:
    """提取脚本意图（前端展示"这个脚本做了什么"）：首个有内容的中文注释行（去 # 与 === 装饰），
    无注释则取首个 print(...) 参数前缀。截断 60 字符。"""
    for line in code.splitlines():
        s = line.strip()
        if s.startswith("#"):
            t = s.lstrip("#").strip(" =—-·")
            if t and len(t) >= 4:
                return t[:60]
    for line in code.splitlines():
        s = line.strip()
        if s.startswith("print(") and '"' in s or s.startswith("print(") and "'" in s:
            inner = s[s.find("(") + 1 : s.rfind(")")]
            t = inner.strip().strip("'\"")
            if t and not t.startswith("f"):
                return t[:60]
    return ""


def _brief_args(name: str, args: dict) -> str:
    """工具参数摘要（前端工具过程展示"具体做了什么"；2026-08-10——原事件只有耗时无参数，
    沙盒脚本内容/SQL/文件名对用户不可见）。仅展示参数，脚本全文仍只在日志。"""
    if not args:
        return ""
    if name == "run_script":
        mode = str(args.get("mode") or "run")
        if mode == "write":
            return f"write {args.get('file')}（{len(str(args.get('code') or ''))} 字符）"
        if mode == "edit":
            return f"edit {args.get('file')}"
        if mode == "grep":
            return f"grep {str(args.get('pattern') or '')[:60]}"
        if mode == "deliver":
            return f"deliver {args.get('file')}"
        f = args.get("file")
        if f:
            return f"run {f}"
        # 2026-08-10：描述脚本做了什么（提取首条意图注释/print 前缀），替代死板的字符数
        code_s = str(args.get("code") or "")
        intent = _script_intent(code_s)
        return f"run 脚本：{intent}" if intent else f"run 脚本（{len(code_s)} 字符）"
    if name == "file_parse":
        return f"{args.get('file_path', '')}（max_chars={args.get('max_chars', '默认')}）"
    if name == "subagent":
        return f"task：{str(args.get('task') or '')[:80]}"
    if name in ("generate_chart", "chart_gen"):
        return str(args.get("chart_label") or args.get("chart_type") or "")[:60]
    if name in ("doc_export", "html_report"):
        return f"「{str(args.get('title') or '')[:50]}」"
    return "，".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:3])


def _brief(result, args: dict, tool_name: str) -> str:
    """从工具结果提取一句话简述（前端工具调用过程展示，写给用户看，按工具定制不重复）。"""
    if not isinstance(result, dict):
        return str(result)[:50]
    if result.get("error"):
        return f"执行失败：{str(result['error'])[:40]}"
    if result.get("partial") and result.get("error_note"):
        return "部分成功（部分条目失败，详见下方）"
    # 按工具定制（结合参数，具体而非模板化）
    if tool_name == "generate_chart":
        label = result.get("chart_label") or ""
        return f"已生成「{label}」图表" if label else "已生成图表"
    if tool_name == "doc_export":
        return f"已生成文档「{result.get('label') or args.get('title') or '文档'}」"
    if tool_name == "file_parse":
        fname = result.get("file_name") or ""
        if fname:
            return f"已解析文件「{fname}」"
        if result.get("sheets"):
            return f"已解析表格，共 {len(result['sheets'])} 个工作表"
        return "已解析上传文件"
    if tool_name == "web_search":
        return f"已联网搜索「{str(args.get('query') or '')[:20]}」，找到 {len(result.get('results') or [])} 条结果"
    if tool_name == "file_search":
        # 2026-09-10：kb_match 下线，统一走 file_search（知识库检索）
        if result.get("mode") == "inventory":
            return "已列出文件库目录清单"
        n = len(result.get("content_matches") or []) + len(result.get("file_matches") or [])
        return f"已检索文件库，命中 {n} 条"
    if tool_name == "image_recognition":
        return f"已识别图片（{result.get('type') or 'auto'}）"
    if tool_name == "image_generation":
        return f"已生成 {len(result.get('images') or [])} 张图片"
    if tool_name == "memory":
        action = str(args.get("action") or "add")
        if action == "add":
            return str(result.get("note") or "已记录记忆")
        if action == "list":
            return f"已读取 {len(result.get('memories') or [])} 条记忆"
        return f"已删除记忆（{result.get('deleted') is True}）"
    if tool_name == "run_script":
        dur = result.get("duration_s")
        return f"已在沙箱执行脚本（{dur}s，退出码 {result.get('exit_code')}）" if dur is not None else "已在沙箱执行脚本"
    # 通用兜底（保持可读，不重复；note 是给模型的内部指令字段，不向用户展示）
    for key in ("chart_label", "label", "file_name"):
        if result.get(key):
            return str(result[key])[:50]
    return "调用完成"


# M1（2026-08-10）：交付类工具清单（与 system.py 产出导向 L40 一致）——
# 调用任一即算"交付进展"；写 work 中间文件（run_script 非 deliver）不计交付。
DELIVERY_TOOLS = {"doc_export", "html_report", "generate_chart"}

# 2026-09-08 护栏重构：失败分类（错误通道改造配套）。
# 分类流：result.error_code（工具显式声明，优先）→ 泛化关键词匹配。
# wait=需要等待（风控/熔断/限流——禁止盲试，提示用户与停止）；retryable=换参数/换链接可重试；
# dead_end=此路不通（权限/不存在/未启用——换方案或如实告知）。
_WAIT_KEYS = ("风控", "冷却", "熔断", "限流", "节流", "请稍后", "恢复", "429", "rate limit", "限制")
_DEAD_KEYS = ("不在授权", "白名单", "未启用", "不存在", "无权", "非法", "只读", "不支持", "已禁用",
              "禁止", "无法访问", "未开放", "无法识别当前用户", "符号链接逃逸")


def classify_error(error_text: str | None, error_code: str | None = None) -> str:
    """失败分类：wait / retryable / dead_end。error_code 显式声明优先于关键词匹配。"""
    if error_code in ("wait", "retryable", "dead_end"):
        return error_code
    t = (error_text or "").lower()
    if any(k in t for k in _WAIT_KEYS):
        return "wait"
    if any(k in t for k in _DEAD_KEYS):
        return "dead_end"
    return "retryable"


def _ask_user_limit(mode: str) -> int:
    """反问硬限（2026-09-08）：quick 每回合 1 次；complex（澄清/取舍阶段）2 次。"""
    return 2 if mode == "complex" else 1


def _round_delivery(outputs_before: int, outputs: list[dict], tool_names: list[str],
                    successful_subagent: bool = False) -> bool:
    """M1：本轮是否产生交付进展——产出物增加，或调用了交付类工具（调用即算，防止
    '调了 html_report 但失败'被误判——工具失败会走 error 分支不计 streak，此处保守从宽）。
    支柱 1（2026-08-10）：子代理成功返回结构化报告也算交付进展——本次重构的目的就是把
    无界探测从主上下文迁到子代理，若不算交付，主循环 8 轮无交付门禁会打断子代理驱动执行；
    防滥用靠子代理自身双层兜底（轮次预算 + streak + 连续失败）。"""
    return ((len(outputs) > outputs_before)
            or any(n in DELIVERY_TOOLS for n in tool_names)
            or successful_subagent)


# D4（2026-08-10）：大内容字段（可能吃掉全部 200K 预算的字段）与元信息字段分级回填——
# meta（work_files/note/exit_code 等）先序列化保底，bulk 按剩余预算截断填充。
# 修复：run_script 的 stdout 一长，dict 末尾的 work_files/note 被整体截掉 → Agent 失去
# "work 目录现有文件"信息，跨轮复用断链。
_BULK_KEYS = {"stdout", "stderr", "rows", "markdown", "content", "chunks", "tables_text"}


def _result_to_text(result) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        # 图表 option 不完整回填（太大），只回填说明
        if "option" in result and "chart_label" in result:
            return json.dumps(
                {k: v for k, v in result.items() if k != "option"}, ensure_ascii=False,
                default=json_default
            )
        bulk = {k: v for k, v in result.items() if k in _BULK_KEYS}
        meta = {k: v for k, v in result.items() if k not in _BULK_KEYS}
        # D4（2026-08-10 v2）：分级截断但保持整体合法 JSON——v1 把 meta 与 bulk 两个
        # json.dumps 直接拼接（{...}{...}），tool_exec 无进展检测 json.loads 抛异常被吞 →
        # round_effective 恒 False → streak 每轮 +1 → 4 轮工具后必然 chat 收尾，
        # 沙盒多轮调试流程被误判终止（真实事故根因，2026-08-10 复现确认）
        budget = _settings.tool_result_max_chars
        # 2026-09-24：所有 dumps 一律带 json_default 兜底——工具结果里出现 Decimal/时间/UUID 等
        # 非原生值时，宁可把它降级成字符串，也不能抛 TypeError 炸掉整轮（当天走查实报的现场）
        meta_s = json.dumps(meta, ensure_ascii=False, default=json_default)
        if len(meta_s) >= budget:
            return meta_s[:budget] + "..."
        out = dict(meta)
        remaining = budget - len(meta_s) - 50  # 预留闭合括号余量
        for k, v in bulk.items():
            s = json.dumps(v, ensure_ascii=False, default=json_default)
            if len(s) <= remaining:
                out[k] = v
                remaining -= len(s)
            else:
                out[k] = s[: max(remaining - 30, 0)] + "...（回填截断）"
                remaining = 0
        return json.dumps(out, ensure_ascii=False, default=json_default)
    return str(result)


def _result_to_text_degraded(result) -> str:
    """支柱 3b（2026-08-10）：单 ask 工具结果累积超限时的降级回填——meta 字段全量
    + stdout/stderr 尾部 2000 字符 + 护栏标记。**必须保持合法 JSON**（D4 v1 拼接 bug 教训：
    非法 JSON 会被无进展检测 json.loads 吞掉 → streak 误增）；_effective 仍能识别 stdout →
    无进展判定不失效。"""
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return str(result)[:2000]
    out = {k: v for k, v in result.items() if k not in _BULK_KEYS}
    out["note"] = f"（总量护栏）{out.get('note', '')}本 ask 工具结果累计超限，已降级回填 stdout/stderr 尾部。".strip()
    for k in ("stdout", "stderr"):
        if isinstance(result.get(k), str) and len(result[k]) > 2000:
            out[k] = f"...（降级回填尾部）\n{result[k][-2000:]}"
        elif result.get(k):
            out[k] = result[k]
    return json.dumps(out, ensure_ascii=False, default=json_default)


async def run_tool_exec(state: AgentState, config: RunnableConfig) -> dict:
    c = config["configurable"]
    events: asyncio.Queue = c["events"]
    qm = get_queue_manager()
    allowed_tools = state.get("allowed_tools") or []

    pending = state.get("pending_tool_calls") or []
    # 空工具兜底：残留状态导致空工具轮时直接收尾，防空循环（正常路径不会到达）
    if not pending:
        logger.warning("tool_exec 收到空工具列表（残留状态？），直接收尾")
        return {"force_final": True}
    messages: list[dict] = list(state.get("messages") or [])
    # D7（2026-08-10）：无进展判定只扫"本轮新增"的 tool 消息（循环前记录起点）——
    # 原实现遍历整份累计 messages，本次 ask 中任意一轮成功过 → streak 恒为 0，4 轮兜底名存实亡
    msg_start = len(messages)
    outputs: list[dict] = list(state.get("round_outputs") or [])
    outputs_before = len(outputs)  # M1：交付进展判定基准（本轮执行前产出数）
    # M1（2026-08-10）：本轮工具名收集（交付进展判定；含失败工具——调用交付工具即算进展）
    tool_names: list[str] = [t.get("function", {}).get("name", "") for t in pending]
    tool_events: list[dict] = list(state.get("tool_events") or [])
    ctx: ToolContext = c["ctx"]
    failures = 0
    # 支柱 3b（2026-08-10）：单 ask 工具结果回填累积护栏（跨 tool_exec 调用累积，
    # 超 ask_tool_chars_max 后后续结果降级 meta+尾 2K——防 21 轮 × 200K 挤爆 1M 上下文）
    ask_tool_chars = state.get("ask_tool_chars") or 0
    # 支柱 1：子代理成功返回报告 = 交付进展（M1 判定扩展）
    delivery_extra = False
    # 2026-08-10：M1 门禁豁免——run_script write/edit 成功（写了 work 中间文件）= "构建轮"，
    # 不累计无交付 streak（38c5f55f 实测：8 轮门禁误伤正在写构建脚本的轮次，用户被迫确认继续）
    work_progress = False
    # 2026-08-20（P3b）：同参重复计数器——call_counts 随 state 跨轮累积（跨 ask 重置，
    # 同 tool_round_count 语义）；had_repeat=本轮存在"完全相同工具+参数"的第 2+ 次调用
    call_counts: dict = dict(state.get("call_counts") or {})
    repeat_streak = state.get("repeat_streak") or 0
    had_repeat = False
    repeat_names: list[str] = []
    # 2026-09-08：反问计数（state.asked_this_round 死字段接线；本批若 ask_user 成功则 +1）
    asked_this_round = int(state.get("asked_this_round") or 0)

    intent_ts = state.get("intent_ts")  # v2：intent_event 时间戳（result_event 补算 duration_s）
    # v2：todo_step 状态累积（步序/清单/修订回退）+ P2/P3 子代理观察标记
    todo_state: dict = dict(state.get("todo_state") or {})
    current_step_index = state.get("current_step_index") or 0
    todo_replan = False
    explore_done = state.get("explore_done", False)
    plan_candidate: str | None = state.get("plan_candidate")
    logger.info("tool_exec 开始 round=%d tools=%s", state.get("tool_round_count"), [t.get("function", {}).get("name") for t in pending])
    for tc in pending:
        name = tc.get("function", {}).get("name", "")
        try:
            args = json.loads(tc.get("function", {}).get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        logger.info("执行工具 %s args=%s", name, json.dumps(args, ensure_ascii=False)[:200])

        # 白名单二次校验（Agent 空间隔离）
        spec = get_tool(name)
        if spec is None or name not in allowed_tools:
            await events.put({"event": "tool", "tool_name": name, "label": tool_label(name),
                              "status": "error", "detail": "工具不在授权白名单"})
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": json.dumps({"error": "E010 工具调用被拒绝：不在授权白名单"}, ensure_ascii=False)})
            tool_events.append(  # L6：白名单拒绝补审计记录（原只发事件不进 tool_events）
                {"tool_name": name, "label": tool_label(name), "status": "error",
                 "brief": "白名单拒绝", "detail": "E010 工具调用被拒绝：不在授权白名单"}
            )
            failures += 1
            continue

        # 2026-09-08 反问硬限：state.asked_this_round 死字段接线——quick 每回合 1 次、
        # complex（澄清/取舍阶段）每回合 2 次；超限直接拒绝（dead_end 分类：换方案=基于已有信息执行）。
        # 走查实况：LLM 连续反问 6 次（提示词"宁多勿少/仍不清可再问" + 无计数可见 + ask_user 每次
        # 回填 ok 被计入有效进展 → 无进展/重复检测永不触发）。
        if name == "ask_user":
            _ask_limit = _ask_user_limit(state.get("mode") or "quick")
            _asked_now = int(state.get("asked_this_round") or 0)
            if _asked_now >= _ask_limit:
                ask_err = (
                    f"你已在本回合询问过用户确认（{_asked_now}/{_ask_limit}）。请不要再次调用 ask_user——"
                    "按照已有信息继续执行：涉及不明确处做合理假设并在最终回复中简要说明；"
                    "或直接输出结果并列出你未确认的假设项。"
                )
                await events.put({"event": "tool", "tool_name": name, "label": tool_label(name),
                                  "status": "error", "detail": ask_err})
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "content": json.dumps({"error": ask_err, "error_code": "dead_end"}, ensure_ascii=False)})
                tool_events.append({"tool_name": name, "label": tool_label(name), "status": "error",
                                    "brief": "反问已达上限", "detail": ask_err, "error_category": "dead_end"})
                continue

        # v2：intent_event/result_event 特判——只广播 SSE 事件（不发 tool 事件/不进工具明细/
        # 不占队列/不参与无进展与交付判定），仅回填极简确认供消息配对；
        # 2026-08-18：同时写入 tool_events（kind 标记）——前端 timeline 持久化（刷新可见完整循环）
        if name in ("intent_event", "result_event"):
            if name == "intent_event":
                intent_ts = time.monotonic()
                await events.put({"event": "intent", "text": str(args.get("text", ""))[:80], "scope": "event"})
                tool_events.append({"kind": "intent", "text": str(args.get("text", ""))[:80], "scope": "event"})
            else:
                dur = round(time.monotonic() - (intent_ts or time.monotonic()), 1)
                await events.put({"event": "result", "text": str(args.get("text", ""))[:120],
                                  "ok": bool(args.get("ok", True)), "duration_s": dur, "scope": "event"})
                tool_events.append({"kind": "result", "text": str(args.get("text", ""))[:120],
                                    "ok": bool(args.get("ok", True)), "duration_s": dur, "scope": "event"})
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "content": json.dumps({"ok": True}, ensure_ascii=False)})
            continue

        # 2026-08-18：图表生成同轮硬上限（prompt 软约束失效时的护栏——实测一轮 16+ 张）
        if name == "generate_chart":
            chart_done = sum(1 for t in tool_events
                             if t.get("tool_name") == "generate_chart" and t.get("status") == "done")
            if chart_done >= _settings.chart_per_round_limit:
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "content": json.dumps(
                                     {"error": f"本轮图表生成已达上限（{_settings.chart_per_round_limit} 张）："
                                      "请收敛为一张图表或先向用户确认需要哪些图，不要重复生成"},
                                     ensure_ascii=False)})
                failures += 1
                continue

        # v2：todo_step 特判——复杂 P6 步骤清单推进（广播 scope="step" 事件，不进工具明细）
        if name == "todo_step":
            action = str(args.get("action") or "")
            step_id = str(args.get("step_id") or "")
            steps = (state.get("plan_json") or {}).get("steps") or []
            if action == "replan":
                # 插话推翻已批准计划 → 回 P5（todo_state 保留已完成项；plan_approval 置空重提）
                await events.put({"event": "result", "text": "已收到要求，回到计划修订",
                                  "ok": True, "duration_s": 0, "scope": "step",
                                  "step_index": state.get("current_step_index") or 0})
                tool_events.append({"kind": "result", "text": "已收到要求，回到计划修订",
                                    "ok": True, "duration_s": 0, "scope": "step",
                                    "step_index": state.get("current_step_index") or 0})
                todo_replan = True
            elif action == "start":
                idx = next((i for i, s in enumerate(steps, 1) if s.get("id") == step_id), 0)
                if idx == 0:
                    messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                     "content": json.dumps({"error": f"step_id {step_id} 不在计划步骤中"}, ensure_ascii=False)})
                    failures += 1
                    continue
                current_step_index = idx
                step = steps[idx - 1]
                await events.put({"event": "intent", "text": f"步骤 {idx}/{len(steps)}：{step['title']}",
                                  "scope": "step", "step_index": idx, "step_id": step_id,
                                  "title": step["title"]})
                tool_events.append({"kind": "intent", "text": f"步骤 {idx}/{len(steps)}：{step['title']}",
                                    "scope": "step", "step_index": idx, "step_id": step_id})
            elif action in ("done", "fail"):
                idx = next((i for i, s in enumerate(steps, 1) if s.get("id") == step_id),
                           state.get("current_step_index") or 0)
                todo_state[step_id] = "done" if action == "done" else "failed"
                await events.put({"event": "result", "text": str(args.get("summary") or "")[:80],
                                  "ok": action == "done", "duration_s": 0, "scope": "step",
                                  "step_index": idx, "step_id": step_id})
                tool_events.append({"kind": "result", "text": str(args.get("summary") or "")[:80],
                                    "ok": action == "done", "duration_s": 0, "scope": "step",
                                    "step_index": idx, "step_id": step_id})
            else:
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "content": json.dumps({"error": f"未知 todo action {action}"}, ensure_ascii=False)})
                failures += 1
                continue
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "content": json.dumps({"ok": True}, ensure_ascii=False)})
            continue

        # 写门禁（两层）：
        # ① v2 复杂任务：计划批准前（clarify/explore/design/tradeoff/approval 阶段）写工具确定性拒绝
        #    ——subagent 豁免（P2/P3 探索设计依赖）；todo_step/intent_event/result_event 为内部工具；
        #    run_script 豁免非 deliver 用法（2026-08-14：探索/设计阶段解析提取必需，沙盒隔离无系统写，
        #    mode=deliver 发布交付物仍拒绝）；拒绝静默（只回给 Agent，不广播 tool 事件——门禁是预期行为，
        #    用户侧展示会造成"计划还没给我就报错"的困惑）
        # ② B8（D20 文字计划）：计划未确认时写工具确定性拒绝（旧流程保留，quick 模式生效）
        pre_approval = (state.get("mode") == "complex"
                        and (state.get("phase") or "") in ("clarify", "explore", "design", "tradeoff", "approval")
                        and spec.write and name not in ("subagent", "todo_step", "intent_event", "result_event"))
        if pre_approval and name == "run_script" and str(args.get("mode") or "run") != "deliver":
            pre_approval = False
        d20_pending = state.get("plan_status") == "pending" and spec.write
        if pre_approval or d20_pending:
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "content": json.dumps({"error": "计划尚未确认：请先确认计划后再执行产出类工具"}, ensure_ascii=False)})
            failures += 1
            continue

        # 2026-08-20（P3b）：同参重复计数——完全相同工具+参数（含失败调用，防同参失败重试
        # 死循环；写门禁拒绝等未真正 dispatch 的不计）
        call_key = f"{name}\0{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        call_counts[call_key] = call_counts.get(call_key, 0) + 1
        if call_counts[call_key] >= 2:
            had_repeat = True
            if name not in repeat_names:
                repeat_names.append(name)
            # G3（全局 Explore 排查）：只读工具同轮同参第 2+ 次调用——不再执行（结果见上方；
            # 与 P3b「同参重试死循环」语义一致；写工具不拦——重试语义合法）
            if not spec.write:
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "content": json.dumps({"error": f"（{name} 已用相同参数执行过，结果见上方；"
                                                               "如需新数据请调整参数或说明新需求）"}, ensure_ascii=False)})
                continue

        queue = get_queue_for(name)
        pos = await qm.queue_position(queue)
        # 2026-08-20（P3a）：subagent 的 start 事件由子代理循环自身广播（subagent.py:407-410
        # 带 subagent_id，含角色/工具数/预算详情）——主循环不再重复发（原双发致前端
        # 时间线两条"子代理"行，用户感知"重复运行"）
        if name != "subagent":
            await events.put(
                {"event": "tool", "tool_name": name, "label": tool_label(name), "status": "start",
                 # 2026-09-24：队列名也出中文（原来直接把 read/default 这种英文 id 摆给用户看）
                 "detail": f"队列 {queue_label(queue)}" + (f"，前面还有 {pos} 个任务" if pos else "，立即执行")}
            )

        dispatch_result = {"queued": False, "position": 0, "duration_s": 0, "result": None}
        try:
            # E-01(数据层)：传 coroutine 工厂（每次新建）——工具异常不再被队列误判 Redis 故障
            dispatch_result = await qm.dispatch(
                queue, lambda: spec.handler(args, ctx)
            )
            result = dispatch_result["result"]
        # 2026-09-15：异常分支**不再计数**——统一由下方 error 分支按失败分类计一次。
        # 原实现在这里 +1、error 分支又 +1 → **抛异常型失败一次就被算成 2**，立刻命中
        # 下方 `failures >= 2 → force_final`（强制收尾）→ 用户实感"工具一失败就直接打断、
        # 连换策略重试的机会都没有"。返回 error dict 的失败（工具自己 catch 转 dict）只 +1 不受影响，
        # 所以此前只在"抛异常"这条路上踩到（外部工具进程缺失 = FileNotFoundError）。
        except asyncio.TimeoutError:
            result = {"error": "E005 工具执行超时（排队过久或执行超时）"}
        except Exception as e:
            result = {"error": f"工具执行失败: {str(e)[:200]}"}

        # 产出物识别须在消息回填前：chart_id 注入工具结果，让 LLM 拿到真实 id
        # （doc_export 内嵌图表依赖 chart_ids；此前 LLM 看不到 id 只能编造 → uuid 列绑定报 DataError → 反复重试重复生成）
        chart_id = None
        if isinstance(result, dict) and "option" in result and result.get("chart_type"):
            # 真实 UUID：与 chart_outputs 表 id 一致（PNG 导出 /chat/charts/{id}/png 与 docx 内嵌 chart_ids 依赖）
            chart_id = str(uuid.uuid4())
            result["chart_id"] = chart_id
            # 同轮图表注册表（DB 落库在轮末 persist_round，doc_export 同轮内嵌先查内存）
            if ctx.chart_registry is not None:
                ctx.chart_registry[chart_id] = {"option": result["option"], "session_id": ctx.session_id}

        # 2026-09-08 反问计数：ask_user 执行完成（用户已答/超时自动提交）即算"问过 1 次"——
        # 运行状态注入给模型自省，下一轮再调就撞硬限
        if name == "ask_user" and not result.get("error"):
            asked_this_round = int(state.get("asked_this_round") or 0) + 1
        text = _result_to_text(result)
        if ask_tool_chars + len(text) > _settings.ask_tool_chars_max:
            text = _result_to_text_degraded(result)
            logger.info("ask 工具结果累积超限降级 round=%d tool=%s used=%d", state.get("tool_round_count"), name, ask_tool_chars)
        ask_tool_chars += len(text)
        messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": text})
        # 支柱 1：子代理成功返回（报告非空且无 error）= 交付进展
        if name == "subagent" and isinstance(result, dict) and not result.get("error") and result.get("report"):
            delivery_extra = True
        # v2 P2/P3 观察（确定性 phase 推进依据）：
        # - wait_all 成功汇合 → 探索完成标记（P2→design）
        # - role=plan 同步返回报告 → 计划候选（P3 解析为 plan_json）
        if name == "subagent" and isinstance(result, dict) and not result.get("error"):
            if str(args.get("mode") or "wait") == "wait_all" and result.get("reports"):
                explore_done = True
                logger.info("P2 探索汇合完成 session=%s", str(ctx.session_id)[:8])
            if args.get("role") == "plan" and result.get("report"):
                plan_candidate = str(result["report"])[:12000]  # plan 报告上限 8000，留余量防截坏 JSON
                logger.info("P3 计划候选收到 session=%s len=%d", str(ctx.session_id)[:8], len(plan_candidate))
        # 2026-08-10：run_script write/edit 成功 = 构建轮（work 中间文件有产出）→ M1 门禁豁免
        if (name == "run_script" and isinstance(result, dict) and not result.get("error")
                and str(args.get("mode") or "run") in ("write", "edit")):
            work_progress = True
        # 2026-08-31（检索误杀修复）：探索/信息获取型工具返回有效内容 = 任务推进——
        # 计入交付进展，防大量文件检索被「连续 8 轮无交付卡点收尾」误杀（探索轮无产出物
        # 但确实在推进；M1 防的 run_script stdout 空转语义保留——run_script 不在豁免集）
        if (name not in ("run_script", "subagent") and isinstance(result, dict) and not result.get("error")
                and any(result.get(k) for k in ("files", "chunks", "blocks",
                                                "sheets", "sheet_summary", "memories", "documents",
                                                "rows", "matches", "results", "markdown", "content", "text"))):
            work_progress = True

        is_partial_err = isinstance(result, dict) and result.get("error") and result.get("partial")
        if is_partial_err:
            # 2026-09-08：部分成功（多条目任务部分失败）——不算失败（不计数/不 error 事件），
            # error 文本并入回填供 LLM 甄别，brief 明示"部分失败"
            result_merged = {
                "error_note": str(result["error"])[:2000],
                "partial": True,
                **{k: v for k, v in result.items() if k not in ("error", "partial")},
            }
            result = result_merged
        if isinstance(result, dict) and result.get("error"):
            # 2026-08-31（错误可见性）：工具失败写日志——此前只走 SSE/DB，故障在日志中不可见
            logger.warning("工具执行失败 tool=%s args=%s err=%s", name,
                           str(args)[:200], str(result["error"])[:200])
            # 2026-09-08 失败分类：等待型（风控/熔断/限流）不计连续失败计数器——
            # 熔断期间一批多条链接"同批多败"不应立即掐断（非死循环信号，是等待信号）
            _cat = classify_error(str(result["error"])[:200], result.get("error_code"))
            if _cat != "wait":
                failures += 1
            # 2026-08-20（P3a）：subagent 强制收尾（带 subagent_id）的 error 事件由子代理
            # 循环自身广播——主循环跳过防双行；失败信息仍完整进 tool_events（output_note 回填）
            if not (name == "subagent" and result.get("subagent_id")):
                await events.put({"event": "tool", "tool_name": name, "label": tool_label(name),
                                  "status": "error", "detail": str(result["error"])[:200]})
            # 2026-08-17（审计缺口）：失败原因进 tool_events（原只发 SSE 不落库——
            # 视频生成失败等用户/日志均不可见）
            tool_events.append({"tool_name": name, "label": tool_label(name), "status": "error",
                                "brief": "失败", "detail": str(result["error"])[:300],
                                "error_category": _cat})
            continue

        # 产出物收集
        if chart_id:
            outputs.append(
                {
                    "type": "chart",
                    "label": result.get("chart_label", ""),
                    "chart_id": chart_id,
                    "chart_type": result.get("chart_type"),
                    "option": result["option"],
                }
            )
            await events.put(
                {
                    "event": "chart",
                    "chart_id": chart_id,
                    "round_id": state.get("round_id", 0),
                    "label": result.get("chart_label", ""),
                    "type": result.get("chart_type"),
                    "option": result["option"],
                }
            )
        if isinstance(result, dict) and result.get("file_path"):
            # 2026-08-10：产出类型三选一——工具自报（video_generate 的 output_type=video）、
            # 按扩展名推断、默认 file。修复：deliver 图片产出 type="file" → 前端走文档预览
            # （OfficeCLI 不支持 jpg → 浏览区图片显示失败）
            _type = result.get("output_type") or _infer_output_type(result["file_path"])
            outputs.append({"type": _type,
                            "label": result.get("label", ""), "file_path": result["file_path"]})
        if isinstance(result, dict) and result.get("images"):
            for img in result["images"]:
                outputs.append({"type": "image", "label": "生成图片", "file_path": img})

        brief_text = _brief(result, args, name)
        # 排查详情（前端工具调用过程展开查看）：run_script 带 stdout 尾部；失败带错误；
        # subagent 带完整 report（2026-08-18：前端直接展示子代理完整回复，随落库持久化）；其余为空
        output_note = ""
        if isinstance(result, dict):
            if result.get("error"):
                output_note = str(result["error"])[:300]
            elif name == "run_script" and (result.get("stdout") or result.get("stderr")):
                # 2026-08-20：失败（exit≠0）时 stdout 常为空——stderr 兜底，否则前端/审计看不到失败原因
                output_note = f"...{str(result.get('stdout') or result.get('stderr'))[-300:]}"
            elif name == "subagent" and result.get("report"):
                output_note = str(result["report"])
        # B7（D18）：summary 是给 LLM 跨轮追问的上下文（≤800 字符，随 tool_events 落库，
        # _load_history 提取注入动态上下文）——run_script 取 stdout 尾段（sql_query 的 preview
        # 分支 2026-09-17 随该工具下线删除）
        summary = ""
        if isinstance(result, dict):
            if result.get("error"):
                summary = str(result["error"])[:800]
            elif name == "run_script" and (result.get("stdout") or result.get("stderr")):
                summary = f"...{str(result.get('stdout') or result.get('stderr'))[-800:]}"
        # 2026-08-20（P3a）：subagent 成功 done 由子代理循环自身广播（subagent.py:239-241
        # 带 subagent_id）——主循环跳过防前端双行；wait_all/poll/早期参数错误等无
        # subagent_id 的结果仍正常广播（tool_exec 侧可见性保留）；tool_events 追加保留
        # （审计 + 刷新重建单一来源）
        if not (name == "subagent" and isinstance(result, dict) and result.get("subagent_id")):
            await events.put(
                {
                    "event": "tool",
                    "tool_name": name,
                    "label": tool_label(name),
                    "status": "done",
                    "detail": f"完成（{dispatch_result.get('duration_s', '?')}s）",
                    "brief": brief_text,
                    "args": _brief_args(name, args),  # 2026-08-10：参数摘要（前端可见"具体做了什么"）
                    "output": output_note,
                }
            )
        tool_events.append(
            {"tool_name": name, "label": tool_label(name), "status": "done", "brief": brief_text,
             "detail": f"完成（{dispatch_result.get('duration_s', '?')}s）", "output": output_note,
             "summary": summary}
        )
        # B2（2026-08-10）：run_script 完成日志（stdout 首尾摘要——T2 预截断后日志量可控；
        # 事故复盘全靠日志，此缺口必须补）
        if name == "run_script" and isinstance(result, dict) and "error" not in result:
            out = str(result.get("stdout") or "")
            wf = result.get("work_files")
            logger.info(
                "run_script 完成 round=%s exit=%s dur=%ss out_head=%s out_tail=%s work_files=%d",
                state.get("tool_round_count"), result.get("exit_code"),
                result.get("duration_s"), out[:200].replace("\n", " "),
                out[-300:].replace("\n", " "), len(wf) if isinstance(wf, list) else 0,
            )

    tool_round_count = (state.get("tool_round_count") or 0) + 1
    force_final = state.get("force_final", False)

    # 无进展检测（2026-08-07 设计；2026-09-08 重构）：本轮工具是否产生"有信息量的进展"。
    # 有效=至少一个工具成功且命中该工具声明的 progress_keys（工具自报，替代旧全局词表——
    # 旧词表与工具返回键脱节：memory/chart 等成功也被判无进展）。
    # 未声明 progress_keys 的工具从宽（成功即进展，防误伤）；**同文本重复**（不同参数返回
    # 相同结果，如熔断期每次同一句"约10分钟后恢复"）计无进展——防"参数换着试、结果一个样"。
    # 部分成功（error+partial）按成功计（成功键 message/file_path 命中）。
    def _effective(r, tool_name: str) -> bool:
        if not isinstance(r, dict):
            return r is not None
        if r.get("error") and not r.get("partial"):
            return False
        keys = (get_tool(tool_name).progress_keys if get_tool(tool_name) else None)
        if keys is None:
            return True  # 未声明=从宽（成功即进展）
        if not keys:
            return False
        return any(r.get(k) for k in keys)

    # D7（2026-08-10）：只扫本轮新增（messages[msg_start:]）——遍历累计消息时
    # 本次 ask 任意一轮成功过 streak 恒为 0，4 轮兜底失效；非 JSON 字符串结果保守计无进展
    # 2026-09-08：同时收集本轮失败分类与重复结果文本（护栏注记输入）
    # tool_call_id → 工具名（本轮 pending 全部执行回填，id 可精确配对）
    name_by_id: dict[str, str] = {t.get("id", ""): t.get("function", {}).get("name", "")
                                  for t in (state.get("pending_tool_calls") or [])}
    fail_cats_this: set[str] = set()
    seen_texts: dict[str, set[str]] = dict(state.get("seen_result_texts") or {})  # tool_name → text 哈希集合（当前 ask 内累积）
    result_repeat = False
    round_effective = False
    unparsed = 0
    for m in messages[msg_start:]:
        if m.get("role") == "tool" and isinstance(m.get("content"), str):
            try:
                parsed = json.loads(m["content"])
            except Exception:
                unparsed += 1
                continue
            tool_name = name_by_id.get(str(m.get("tool_call_id") or ""), "")
            if tool_name and isinstance(parsed, dict) and parsed.get("error") and not parsed.get("partial"):
                fail_cats_this.add(classify_error(str(parsed.get("error"))[:200], parsed.get("error_code")))
            if _effective(parsed, tool_name):
                round_effective = True
                if tool_name:
                    # 同文本重复检测：同一工具的结果文本（成功）在本 ask 内出现过 → 记重复
                    # （"参数换着试、结果一个样"=无新信息；失败等值文本由失败分类 wait 覆盖）
                    text_hash = hashlib.sha1(m["content"].encode("utf-8")).hexdigest()[:16]
                    prev = seen_texts.setdefault(tool_name, set())
                    if text_hash in prev:
                        result_repeat = True
                    else:
                        prev.add(text_hash)
    if len(seen_texts) > 2000:
        seen_texts.clear()  # 上限防膨胀（文本哈希极短，仅兜底）
    if unparsed:
        # 2026-08-10 事故复盘：D4 v1 拼接非 JSON 产物 → 此处静默吞掉 → streak 误增 → 4 轮 chat 收尾。
        # 保持日志可见（工具结果应为合法 JSON；_result_to_text 已保证）
        logger.warning("本轮 %d 条工具结果非 JSON（无进展判定受影响，round=%d）", unparsed, state.get("tool_round_count"))
    no_progress_streak = (state.get("no_progress_streak") or 0) + 1 if not round_effective else 0

    # 2026-09-08：失败分类跨轮计数（wait/retryable/dead_end 各自独立；有进展轮清零=不连续）
    fail_streaks: dict[str, int] = dict(state.get("fail_streaks") or {})
    if round_effective:
        fail_streaks = {}
    for cat in fail_cats_this:
        fail_streaks[cat] = int(fail_streaks.get(cat) or 0) + 1
        logger.info("失败分类计数 +=1 cat=%s total=%d round=%d", cat, fail_streaks[cat], tool_round_count)

    if failures >= 2:
        force_final = True
        await events.put({"event": "tool", "tool_name": "__system__", "label": "系统",
                          "status": "error", "detail": "工具执行异常，进入收尾"})

    # M1（2026-08-10）：无交付进展检测 + 阶梯收敛——现状三条防线只认"有无输出"不认"有无交付"
    # （run_script 每轮有 stdout → no_progress_streak 恒 0 → 21 轮零产出不被拦，真实事故根因）。
    # 交付进展 = 产出物增加或调用了交付类工具（写 work 中间文件不计交付）。
    no_delivery_streak = (state.get("no_delivery_streak") or 0) + 1 \
        if not (_round_delivery(outputs_before, outputs, tool_names, delivery_extra) or work_progress) else 0

    # M1 阶梯一：连续 N 轮无交付 → 注入收敛注记（一次性，agent_llm 注入当前轮 user 消息，不打断）
    converge_note: str | None = None
    # 2026-09-08 失败分类注记（一次性，== 阈值触发；触发后靠自检提示+无进展兜底）：
    # 等待型=风控/熔断/限流——禁止盲试；可调整型=已排除一种假设；死路型=此路不通要换方案
    if not force_final and fail_streaks.get("wait") == 2:
        converge_note = (
            "【等待提示】检测到连续 2 次失败均为等待型（风控/冷却/限流类——再次调用短时间内结果一样）。"
            "**请停止盲试**：先输出一行自检结论（任务是否完成/还差什么），向用户如实说明"
            "「当前抓取/查询通道处于冷却期，约 N 分钟后恢复，届时可继续」，然后结束本轮。"
        )
        logger.info("等待型失败注记触发 round=%d wait=%d", tool_round_count, fail_streaks["wait"])
    if not force_final and fail_streaks.get("retryable") == 2:
        converge_note = (
            "【收敛提示】检测到连续 2 次失败属于同因可调型（换参数/换链接可解决）。"
            "先输出一行自检结论，然后**换一种切实不同的方案**（调整参数/换工具/降级处理），"
            "不要再原样重试同一种调用。"
        )
        logger.info("可调整型失败注记触发 round=%d retry=%d", tool_round_count, fail_streaks["retryable"])
    if not force_final and fail_streaks.get("dead_end") == 1:
        converge_note = (
            "【方案提示】该调用返回的是不可修复错误（权限/不存在/未启用类）——此路不通。"
            "先输出一行自检结论，然后换替代方案，或如实向用户说明该方案不可行，不要重复尝试。"
        )
        logger.info("死路型失败注记触发 round=%d", tool_round_count)
    if not force_final and result_repeat:
        converge_note = (
            "【重复提示】检测到你调用了多个参数但返回内容与之前相同——没有新信息。"
            "先输出一行自检结论：任务是否已完成？未完成则说明出现的重复与你的判断，"
            "然后做一步真正不同的推进（换数据源/换工具/直接总结），不要继续重复返回相同结果。"
        )
        logger.info("同文本重复注记触发 round=%d", tool_round_count)
    if not force_final and no_delivery_streak == _settings.delivery_stall_note_rounds:
        converge_note = (
            f"【收敛提示】已连续多轮工具调用未产生交付物（文件/图表）——"
            "先输出一行自检结论（任务是否已完成？还差什么？）。然后按需二选一："
            "① 若任务需要文件交付：本轮调用 doc_export / html_report / generate_chart "
            "完成交付，或在 run_script 中用 mode=deliver 发布工作目录中已完成文件；"
            "② 若任务不需要文件交付：直接输出最终回答结束；"
            "确需继续探索：给出剩余不超过 2 轮的完成计划再执行。"
        )
        logger.info("无交付收敛注记触发 round=%d streak=%d", tool_round_count, no_delivery_streak)

    # M1 阶梯二（v2 改造）：连续 N 轮无交付 → 停止盲试收尾，chat 节点按卡点报告口径汇报
    # （原行为复用 checkpoint 弹确认卡——v2 框架 checkpoint 卡已整体移除，可见性由事件广播保证）
    if not force_final and no_delivery_streak >= _settings.delivery_stall_final_rounds:
        recent = "、".join(dict.fromkeys(e.get("tool_name", "") for e in tool_events[-3:])) or "—"
        converge_note = (
            f"【卡点报告】已连续多轮工具调用未产出交付物（当前产出 {len(outputs)} 项；"
            f"最近步骤：{recent}）。先输出一行自检结论，然后停止盲试——向用户如实汇报卡点、"
            "已排除的假设与建议的下一步，不要继续调用工具。"
        )
        force_final = True
        logger.info("无交付卡点收尾触发 round=%d streak=%d", tool_round_count, no_delivery_streak)

    # 2026-08-20（P1-①）：轮末探测中断队列非空 → 路由优先回 agent_llm 消费（防 cap/
    # force_final 收尾分支吞掉工具执行期间到达的插话——走查"插话消失、工具调用结束
    # 无收到"根因；redis_llen 内存降级路径天然兼容）
    interrupts_pending = False
    try:
        from app.core.redis import redis_llen

        interrupts_pending = (await redis_llen(f"interrupt:{state.get('session_id', '')}")) > 0
    except Exception as e:
        logger.warning("中断队列探测失败 session=%s err=%s", state.get("session_id", ""), e)

    # 2026-08-20（P3b + 走查修正）：软性同参重复拦截——本轮存在"完全相同工具+参数"的
    # 第 2+ 次调用即 streak+1，否则清零。
    # 原条件要求"且无新交付"——被每次生成新文件（doc_export 随机文件名）绕过：走查实测
    # 同任务连跑 4 轮各出新 docx，delivery 判定恒真 → streak 恒 0 → 拦截永不触发。
    # "完全相同参数"是硬判据：合法"改参数重试"（08-10 强化迭代）参数必变 → 不同 key 不计数。
    # 2026-09-08：同文本重复（参数不同但结果相同）并入同参重复判定
    repeat_streak = repeat_streak + 1 if (had_repeat or result_repeat) else 0

    # P3b 阶梯一：连续 repeat_stall_note_rounds 轮同参重复且零进展 → 收敛注记（不打断）
    if not force_final and repeat_streak == _settings.repeat_stall_note_rounds:
        names = "、".join(repeat_names) or "—"
        converge_note = (
            f"【重复收敛提示】你已连续多轮重复调用相同参数或返回相同结果的工具（{names}），"
            "未产生新信息——先输出一行自检结论，然后：若依赖同一结果请直接基于已有结果输出最终回答；"
            "确需重跑必须说明新目标差异，且最多再试一轮。"
        )
        logger.info("同参重复收敛注记触发 round=%d streak=%d names=%s", tool_round_count, repeat_streak, names)

    # P3b 阶梯二：连续 repeat_stall_final_rounds 轮 → 卡点收尾（停止盲试向用户汇报，
    # 复用 M1 阶梯二机制走 chat 收尾）
    if not force_final and repeat_streak >= _settings.repeat_stall_final_rounds:
        names = "、".join(repeat_names) or "—"
        converge_note = (
            f"【卡点报告】你已连续多轮重复调用相同参数或返回相同结果的工具（{names}）"
            "且无新产出——先输出一行自检结论，然后停止盲试，向用户如实汇报当前进展、"
            "重复原因与建议的下一步，不要继续调用工具。"
        )
        force_final = True
        logger.info("同参重复卡点收尾触发 round=%d streak=%d", tool_round_count, repeat_streak)

    # 2026-08-20（走查修正）：计划任务全部完成 → 确定性收敛——原 task_progress 只提示
    # 不阻断（走查实测：s1-s3 全部 done 后 agent 无用户指示连跑 5 轮"复测/验证"）。
    # 条件：plan_tasks 非空且全部完成（启发式 done==len）+ 无插话待消费（有插话=用户
    # 追加指示，不强制收尾）；第一轮注入收敛注记，连续 2 轮仍未收尾 → force_final。
    from app.agent.plan_tasks import task_progress

    plan_done_streak = state.get("plan_done_streak") or 0
    plan_tasks = state.get("plan_tasks") or []
    if plan_tasks and not interrupts_pending:
        _prog = task_progress(plan_tasks, outputs, tool_events)
        _m = re.match(r"任务进度：(\d+)/(\d+) 已完成", _prog)
        if _m and int(_m.group(1)) == int(_m.group(2)) > 0:
            plan_done_streak += 1
            if not force_final and plan_done_streak >= 1:
                converge_note = (
                    "【计划完成】已确认计划的全部步骤均已执行完成——直接基于已有结果输出最终回答结束，"
                    "不要开始新任务、不要重复测试或复测（用户有新要求会另行指示）。"
                )
                logger.info("计划完成收敛注记触发 round=%d streak=%d", tool_round_count, plan_done_streak)
            if not force_final and plan_done_streak >= 2:
                force_final = True
                logger.info("计划完成强制收尾触发 round=%d streak=%d", tool_round_count, plan_done_streak)
        else:
            plan_done_streak = 0
    else:
        plan_done_streak = 0

    # v2：todo_step replan → 回 P5（批准卡重提；todo_state 保留已完成项）
    replan_updates: dict = {}
    if todo_replan:
        replan_updates = {"plan_approval": "", "phase": "approval"}
        force_final = False

    return {
        "messages": messages,
        "round_outputs": outputs,
        "tool_events": tool_events,
        "tool_round_count": tool_round_count,
        "no_progress_streak": no_progress_streak,
        "no_delivery_streak": no_delivery_streak,
        # 2026-08-14 修复：intent_ts 必须随返回值落 state——原只改局部变量，
        # 跨轮 result_event 补算 duration 恒为 0（用户实况：交付结果显示 0s）
        "intent_ts": intent_ts,
        "converge_note": converge_note,
        "force_final": force_final,
        "ask_tool_chars": ask_tool_chars,
        "intent_ts": intent_ts,
        "todo_state": todo_state,
        "current_step_index": current_step_index,
        "explore_done": explore_done,
        "plan_candidate": plan_candidate,
        **replan_updates,
        # 2026-08-20（P1-①）：中断队列探测结果（graph._route_after_tool 读）
        "interrupts_pending": interrupts_pending,
        # 2026-08-20（P3b）：同参重复计数器状态（跨轮累积）
        "call_counts": call_counts,
        "repeat_streak": repeat_streak,
        # 2026-09-08：失败分类计数（wait/retryable/dead_end）与同文本结果哈希集（当前 ask 内）
        "fail_streaks": fail_streaks,
        "seen_result_texts": seen_texts,
        # 2026-09-08：反问计数（state.asked_this_round 死字段接线——quick 1/complex 2 硬限）
        "asked_this_round": asked_this_round,
        # 2026-08-20（走查修正）：计划全部完成后的未收尾轮数（≥2 强制收尾）
        "plan_done_streak": plan_done_streak,
        # 2026-08-07：执行完消费 pending_tool_calls——残留会导致路由误判重复执行旧工具
        # → tool 消息错配 → DeepSeek 400
        "pending_tool_calls": [],
    }
