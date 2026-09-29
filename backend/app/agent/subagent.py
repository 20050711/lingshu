"""支柱 1（2026-08-10）：子代理执行循环；v2（2026-08-14）角色化 + 真后台化。

- 独立消息列表（sub_msgs，原生 dict 含 reasoning_content 成对——DeepSeek 协议）；
  独立轮次预算（subagent_max_rounds=8）+ 无进展 streak + 连续失败强制出报告；
  独立内部累积护栏（subagent_result_chars_max）。
- 子代理事件全部带 subagent_id（SSE 透传，前端分组渲染）。
- 能力面收窄：只读工具 + run_script；产出/交付类硬拒；run_script deliver 在子代理 ctx 下禁用。
- CancelledError 全链路透传（断连级联取消，queue 租约 finally 释放）。

v2 角色化（设计书 §八）：
- explore：纯只读（无 run_script），输出结构化调研结论
- plan：无工具，只输出计划 JSON
- execute：只读+沙盒+交付类（doc_export/html_report/generate_chart）
- generic：现状（只读+run_script，交付硬拒）

v2 后台化（设计书 §十三）：
- mode="wait"（默认）：同步阻塞主循环（quick 自用/单派）
- mode="background"：registry.start 独立任务立即返回；完成回调推 progress 事件 + interrupt 通知
- mode="wait_all"：阻塞汇合一批 background 子代理（超时不取消，返回 running 状态）
- mode="poll"：查单个子代理状态；resume_id 续聊已完成子代理（保留其上下文）
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from dataclasses import replace

from app.agent.prompts.subagent import subagent_prompt_for
from app.agent.queue.queue_manager import get_queue_manager
from app.agent.subagent_registry import SubagentTask, get_subagent_registry
from app.agent.tools import ToolContext, get_queue_for, get_tool, tool_label, tools_to_schemas
from app.core.config import get_settings
from app.core.json_safe import json_default
from app.core.logging import get_logger

logger = get_logger("agent.subagent")
_settings = get_settings()

# 子代理可用工具集（能力面收窄：只读 + 沙盒脚本；产出/交付/记忆类硬拒）
# skill_read：2026-08-26（Q8）execute 子代理执行技能脚本需按名获取 SKILL.md 正文
_SUBAGENT_TOOLS = {
    "web_search", "file_search", "file_parse", "image_recognition",
    "read_output", "run_script", "skill_read",
    # 2026-09-17（用户决策）：补只读媒体两件套（看视频/听录音做分析）。
    # 不加的：ask_user/intent_event/result_event/todo_step（交互面，子代理不反问不广播）、
    # memory（写）、产出类（doc_export/html_report/generate_chart/image_generation/video_generate）、
    # MCP 外部工具（它们走「运维 active ∧ 员工个人启用」双闸门，
    # 而子代理工具面**不过**这道闸门，直接加会绕过员工的启用开关；定时 AI 回复同此）
    "video_understand", "audio_transcribe",
}

# v2：角色工具面（plan 无工具纯输出 JSON；execute 含交付类）
ROLE_TOOLS: dict[str, set[str]] = {
    "generic": set(_SUBAGENT_TOOLS),
    "explore": _SUBAGENT_TOOLS - {"run_script"},
    "plan": set(),
    "execute": _SUBAGENT_TOOLS | {"doc_export", "html_report", "generate_chart"},
}

# 有效输出判定兜底键（仅用于"工具未注册 / 未声明 progress_keys / 调用方没传工具名"的场合）
_EFFECTIVE_KEYS = ("rows", "outputs", "markdown", "content", "text", "images",
                   "stdout", "file_path", "matches", "results", "ok", "report", "reports")


def _effective_result(r, tool_name: str = "") -> bool:
    """工具结果是否算"有效输出"（决定 failures/no_progress 计数，进而决定是否熔断）。

    2026-09-22（走查实锤）：原实现只看上面那份硬编码全局键列表，与各工具
    注册时声明的 progress_keys 已经漂移——file_search（content_matches/file_matches/
    folder_matches/title_matches）、file_parse 的
    xlsx 分支（sheets/sheet_summary）**一个都不在列表里**，于是拿回大量数据的成功调用被判
    "无有效输出"：一轮两个 file_search 即 failures>=2 熔断，子代理第 1 轮被强杀（观感
    "沙盒脚本的处理直接中断"），随后收尾兜底把工具调用当正文发进了群。
    改为与主循环 tool_exec._effective 同源：按工具名取注册时声明的 progress_keys。
    """
    if not isinstance(r, dict):
        return r is not None
    if r.get("error") and not r.get("partial"):
        return False
    spec = get_tool(tool_name) if tool_name else None
    keys = spec.progress_keys if (spec is not None and spec.progress_keys is not None) else _EFFECTIVE_KEYS
    if not keys:
        return False
    return any(r.get(k) for k in keys)


def _result_to_text(result, budget: int) -> str:
    """子代理版结果回填（对齐 D4 分级截断语义）：meta 保底 + stdout/stderr 尾截断，**合法 JSON**。

    D4 v1 教训：dumps 后硬截断会产生非法 JSON → 无进展检测 json.loads 被吞 → 误判。
    此处预算感知：stdout/stderr 尾部长度按剩余预算分配，保证整体 json.dumps ≤ budget。
    """
    if isinstance(result, str):
        return result[:budget]
    if not isinstance(result, dict):
        return str(result)[:budget]
    meta = {k: v for k, v in result.items() if k not in ("stdout", "stderr")}
    # 2026-09-24：同 tool_exec——一律带 json_default 兜底（子代理内部工具结果同样可能带 Decimal 等）
    meta_s = json.dumps(meta, ensure_ascii=False, default=json_default)
    if len(meta_s) >= budget:
        return meta_s[:budget]
    out = dict(meta)
    remaining = budget - len(meta_s) - 24  # 预留闭合余量
    mark = "...（子代理内部护栏截断）\n"
    for k in ("stdout", "stderr"):
        if not isinstance(result.get(k), str) or not result[k]:
            continue
        s = result[k]
        tail_n = min(max(remaining - len(mark) - 4, 0), len(s))
        out[k] = s if len(s) <= tail_n else mark + s[-tail_n:]
        remaining -= len(json.dumps(out[k], ensure_ascii=False, default=json_default))
    return json.dumps(out, ensure_ascii=False, default=json_default)


def _resolve_subagent_tools(role: str, args_tools: object) -> list[str]:
    """args.tools 白名单子集解析：仅允许该角色工具面内的工具；未提供=角色全集。"""
    role_set = ROLE_TOOLS.get(role, _SUBAGENT_TOOLS)
    if args_tools:
        given = [str(t) for t in args_tools if isinstance(t, str)]
        allowed = sorted(t for t in given if t in role_set)
        if not allowed:
            return []
    else:
        allowed = sorted(role_set)
    return allowed


async def _prune_unpaired(sub_msgs: list[dict]) -> list[dict]:
    """对齐主循环 _prune_unpaired_tools：删未配对 tool_calls/tool 消息（DeepSeek 400 防护）。"""
    out: list[dict] = []
    pending_tc_ids: set[str] = set()
    for m in sub_msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            out.append(m)
            pending_tc_ids = {tc.get("id") for tc in m["tool_calls"] if tc.get("id")}
        elif m.get("role") == "tool":
            if m.get("tool_call_id") in pending_tc_ids:
                out.append(m)
                pending_tc_ids.discard(m.get("tool_call_id"))
        else:
            out.append(m)
    return out


async def _finalize_report(llm, sub_msgs: list[dict], forced: bool) -> str:
    """强制收尾路径：调一次 LLM 生成结构化报告（正常路径直接用最后一条 assistant content）。"""
    try:
        from app.agent.nodes.chat import _prune_unpaired_tools

        msgs = [*_prune_unpaired_tools(sub_msgs),
                {"role": "user",
                 "content": "请给出最终报告（≤500 字）：已完成事项、关键结论/数据、产出文件（子目录路径）、"
                            "未完成/失败事项、下一步建议。只输出报告正文——**本轮没有可用工具**，"
                            "只能用已获得的信息作答，禁止输出任何工具调用格式（XML/函数标记）。"}]
        msg = await llm.ainvoke(msgs, tools=None)
        return (msg.get("content") or "（子代理未能生成报告）")[:2000]
    except Exception as e:
        logger.warning("子代理收尾报告失败: %s", str(e)[:100])
        return f"（子代理异常收尾：{str(e)[:200]}）"


async def run_subagent_loop(
    args: dict,
    sub_ctx: ToolContext,
    llm,
    events: asyncio.Queue,
    sub_id: str,
    sub_msgs: list[dict],
    schemas: list[dict] | None,
    allowed: list[str],
) -> dict:
    """子代理执行循环（wait 同步路径与 background 后台任务共用）。

    返回 {"report","rounds_used","work_files","subagent_id","error","note"}。
    """
    qm = get_queue_manager()
    rounds = 0
    failures = 0
    no_progress = 0
    force_exit = False
    try:
        for rounds in range(1, _settings.subagent_max_rounds + 1):
            msg = await llm.ainvoke(sub_msgs, tools=schemas)
            sub_msgs.append(msg)
            sub_msgs = await _prune_unpaired(sub_msgs)
            calls = msg.get("tool_calls") or []
            if not calls:
                break
            round_effective = False
            for tc in calls:
                name = tc.get("function", {}).get("name", "")
                if name not in allowed:  # 幻觉越权二次校验（对齐主循环白名单逻辑）
                    await events.put({"event": "tool", "tool_name": name, "label": tool_label(name),
                                      "status": "error", "subagent_id": sub_id, "detail": "子代理越权工具拒绝"})
                    sub_msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                     "content": json.dumps({"error": "E010 工具调用被拒绝：不在子代理白名单"}, ensure_ascii=False)})
                    failures += 1
                    continue
                try:
                    targs = json.loads(tc.get("function", {}).get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    targs = {}
                from app.agent.nodes.tool_exec import _brief_args

                await events.put({"event": "tool", "tool_name": name, "label": tool_label(name),
                                  "status": "start", "subagent_id": sub_id, "queue": get_queue_for(name),
                                  "detail": "子代理内部执行", "args": _brief_args(name, targs)})
                try:
                    dr = await qm.dispatch(get_queue_for(name), lambda: get_tool(name).handler(targs, sub_ctx))
                    result = dr.get("result")
                except Exception as e:
                    result = {"error": f"子代理工具执行失败: {str(e)[:200]}"}
                text = _result_to_text(result, _settings.subagent_result_chars_max)
                if len(text) > _settings.subagent_result_chars_max:
                    text = text[:_settings.subagent_result_chars_max]
                sub_msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": text})
                if _effective_result(result, name):
                    round_effective = True
                else:
                    failures += 1
                # 子代理内部工具事件（带 subagent_id）
                if isinstance(result, dict) and result.get("error"):
                    await events.put({"event": "tool", "tool_name": name, "label": tool_label(name),
                                      "status": "error", "subagent_id": sub_id, "detail": result["error"][:200]})
                else:
                    out_tail = str(result.get("stdout") or "")[-300:] if isinstance(result, dict) else ""
                    await events.put({"event": "tool", "tool_name": name, "label": tool_label(name),
                                      "status": "done", "subagent_id": sub_id,
                                      "detail": f"完成（{dr.get('duration_s', '?')}s）",
                                      "output": f"...{out_tail}" if out_tail else ""})
            # 无进展检测（子代理自带，主循环门禁不覆盖子代理内部）
            if not round_effective:
                no_progress += 1
                if no_progress >= _settings.subagent_no_progress_limit or failures >= 2:
                    force_exit = True
                    break
    except asyncio.CancelledError:
        raise  # 断连级联取消：全链路透传（queue 租约在 qm.dispatch 的 finally 释放）
    except Exception as e:
        logger.warning("子代理循环异常 sub=%s: %s", sub_id, str(e)[:150])
        force_exit = True

    # 报告：正常路径用最后一条 assistant content；强制/异常路径调收尾 LLM
    last_content = ""
    for m in reversed(sub_msgs):
        if m.get("role") == "assistant" and not m.get("tool_calls") and (m.get("content") or "").strip():
            last_content = m["content"]
            break
    if force_exit or not last_content:
        report = await _finalize_report(llm, sub_msgs, forced=force_exit)
    else:
        # v2：plan 角色输出计划 JSON——2000 字截断会截坏 JSON（解析失败重试死循环），
        # plan 角色给大上限；其余角色保持 ≤2K 报告约定
        cap = 8000 if str(args.get("role") or "") == "plan" else 2000
        report = last_content[:cap]

    # 事件：subagent done（2026-08-18：output 携带完整 report——前端直接展示给用户）
    await events.put({"event": "tool", "tool_name": "subagent", "status": "done",
                      "subagent_id": sub_id, "detail": f"完成（{rounds} 轮）",
                      "brief": "子代理完成", "output": report})
    logger.info("子代理完成 sub=%s rounds=%d failures=%d force=%s report_len=%d",
                sub_id, rounds, failures, force_exit, len(report))
    return {
        "report": report,
        "rounds_used": rounds,
        "work_files": _work_files_of(sub_ctx),
        "subagent_id": sub_id,
        "error": None if not force_exit else "子代理强制收尾（轮次/无进展/失败）",
        "note": "子代理已返回结构化报告（≤2K）。若任务需要文件交付，请用 run_script mode=deliver 发布其产物或直接输出报告结论。",
    }


async def _resolve_llm(ctx: ToolContext) -> object:
    """LLM 实例：默认主 LLM 同档；subagent_use_aux=True 时尝试 llm_aux 档（task key "subagent"）。"""
    llm = ctx.llm
    if _settings.subagent_use_aux:
        try:
            from app.agent.llm_client import DeepSeekLLM
            from app.services.config_service import get_aux_model_cfg_for_task

            cfg = await get_aux_model_cfg_for_task(ctx.department_id, ctx.user_role, "subagent")
            if cfg and cfg.get("model"):
                llm = DeepSeekLLM(role=ctx.user_role or "employee",
                                  model=str(cfg["model"]), effort=str(cfg.get("effort") or ""),
                                  platform=str(cfg.get("platform") or "deepseek"),
                                  thinking=cfg.get("thinking"))  # 2026-08-12 思考三选项透传
        except Exception as e:
            logger.warning("子代理 llm_aux 解析失败（回退主 LLM）: %s", str(e)[:100])
    return llm


def _build_initial_msgs(task: str, sub_ctx: ToolContext, role: str) -> list[dict]:
    return [
        {"role": "system", "content": subagent_prompt_for(role)},
        {"role": "user", "content": f"{task}\n\n工作子目录：work/{sub_ctx.work_subdir}/（用 run_script 时 cwd 即此目录）"},
    ]


async def _background_wrapper(task_rec: SubagentTask, coro, events: asyncio.Queue) -> None:
    """后台任务包装：跑 loop → 写回注册表 → progress 事件 + interrupt 通知。"""
    try:
        result = await coro
        task_rec.status = "done"
        task_rec.report = result.get("report") or ""
        task_rec.error = result.get("error")
        task_rec.rounds_used = result.get("rounds_used") or 0
        progress_status = "done"
        summary = (task_rec.report or "")[:120]
    except asyncio.CancelledError:
        task_rec.status = "cancelled"
        progress_status = "failed"
        summary = "子代理被取消"
    except Exception as e:
        task_rec.status = "failed"
        task_rec.error = str(e)[:200]
        progress_status = "failed"
        summary = f"子代理异常：{str(e)[:100]}"
    task_rec.finished_at = time.monotonic()
    try:
        await events.put({"event": "progress", "kind": "subagent", "agent": task_rec.id,
                          "status": progress_status, "summary": summary})
    except Exception:
        pass
    # 完成通知入中断队列（当前 ask 的下一回合或下个 ask 消费）
    try:
        from app.core.redis import redis_rpush

        await redis_rpush(f"interrupt:{task_rec.session_id}",
                          json.dumps({"type": "subagent", "agent": task_rec.id, "summary": summary},
                                     ensure_ascii=False))
    except Exception as e:
        logger.warning("子代理完成通知入队失败 sub=%s err=%s", task_rec.id, str(e)[:100])
    logger.info("后台子代理收尾 sub=%s status=%s", task_rec.id, task_rec.status)


async def run_subagent(args: dict, ctx: ToolContext) -> dict:
    """子代理执行入口（工具 handler）。"""
    # 入口断言：仅主循环（run_agent 补传 events/llm）可调用；只读工具桥路径天然免疫
    if ctx.events is None or ctx.llm is None:
        return {"error": "子代理需要 events/llm 通道（仅主 Agent 循环可调用）"}
    if not _settings.subagent_enabled:
        return {"error": "子代理已禁用（subagent_enabled=false）"}
    task = str(args.get("task") or "").strip()
    role = str(args.get("role") or "generic")
    if role not in ROLE_TOOLS:
        return {"error": f"未知子代理角色 {role}（可选：generic/explore/plan/execute）"}
    mode = str(args.get("mode") or "wait")
    if mode not in ("wait", "background", "wait_all", "poll"):
        return {"error": f"未知子代理 mode {mode}（可选：wait/background/wait_all/poll）"}
    # task 仅派发模式（wait/background）必填；wait_all/poll 只查询
    if not task and mode in ("wait", "background"):
        return {"error": "subagent 需要 task 参数（目标态指令：做什么/产出放哪/何时停止）"}

    registry = get_subagent_registry()

    # 续聊/查状态：resume_id 指向已完成子代理 → 保留其 sub_msgs 继续（wait 模式）
    resume_id = str(args.get("resume_id") or "").strip()
    resumed_msgs: list[dict] | None = None
    if resume_id:
        prev = registry.get(resume_id)
        if prev is None:
            return {"error": "子代理上下文不存在（服务重启或已过期），请重新派发"}
        if prev.status == "running":
            return {"subagent_id": resume_id, "status": "running",
                    "note": "该子代理仍在执行中，可稍后 poll 或 wait_all 汇合"}
        # 已完成/失败：续聊——保留上下文，追加新任务
        # 踩坑：续聊前必须剪掉未配对 tool_calls 尾部（原上下文可能以 assistant(tool_calls)
        # 结尾，直接续 → DeepSeek 400 insufficient tool messages）
        resumed_msgs = await _prune_unpaired(list(prev.sub_msgs))
        prev.status = "running"  # 防止并发重复续聊；续聊结束时由 wrapper/返回值更新
        prev.finished_at = None

    if mode == "wait_all":
        ids = [str(i).strip() for i in (args.get("ids") or []) if str(i).strip()]
        if not ids:
            return {"error": "wait_all 需要 ids 参数（一批 background 子代理 id）"}
        reports = await registry.wait_all(ids)
        ok_n = sum(1 for r in reports if r["status"] == "done")
        return {"reports": reports, "ok_count": ok_n, "total": len(ids),
                "note": "已汇合各子代理报告（见 reports）；仍在执行的条目状态为 running，可稍后 poll。"}

    if mode == "poll":
        ids = [str(i).strip() for i in (args.get("ids") or []) if str(i).strip()] or ([resume_id] if resume_id else [])
        if not ids:
            return {"error": "poll 需要 ids 或 resume_id 参数"}
        out = []
        for sid in ids:
            t = registry.get(sid)
            if t is None:
                out.append({"subagent_id": sid, "status": "failed", "report": "",
                            "error": "子代理上下文不存在（服务重启或已过期），请重新派发"})
            else:
                out.append({"subagent_id": sid, "status": t.status, "report": t.report or "",
                            "rounds_used": t.rounds_used, "error": t.error})
        return {"reports": out, "note": "见 reports 字段"}

    # wait / background：常规派发
    idx = ctx.subagent_counter + 1
    sub_id = resume_id or f"s-{idx}"
    # 踩坑：必须自增父 ctx 计数器——同轮连续派多个子代理时都从 counter+1 取号，
    # 全部撞名 s-1（注册表覆盖 + resume 续错上下文 + wait_all 汇错报告）
    ctx.subagent_counter = idx
    # F1（红队二次，2026-08-18）：work_subdir 白名单——原只 strip("/") 不拒 ".."，
    # 与 script_sandbox._work_dir 拼接可逃逸沙盒根到宿主任意目录（红队链 2：grep /data/backups）。
    # 白名单正则拒绝 .. / 与绝对路径；平台自动命名 sub_{idx} 天然合法，LLM 自造非法名返回错误可自纠。
    raw_subdir = str(args.get("work_subdir") or f"sub_{idx}").strip("/")
    if not re.fullmatch(r"^[a-z0-9_\-]{1,64}$", raw_subdir):
        return {"error": f"work_subdir 非法：仅允许 1-64 位小写字母/数字/下划线/连字符"
                         f"（收到 {raw_subdir[:80]!r}）——请改用合法子目录名或省略由系统自动命名"}
    sub_ctx = replace(ctx, subagent_id=sub_id,
                      work_subdir=raw_subdir,
                      subagent_counter=idx)
    events = sub_ctx.events

    # 白名单子集（越权拒绝；plan 角色无工具）
    allowed = _resolve_subagent_tools(role, args.get("tools"))
    if not allowed and role != "plan":
        return {"error": "subagent 工具白名单为空——仅允许角色工具面内工具（产出/交付类硬拒）"}
    schemas = tools_to_schemas(allowed) if allowed else None

    llm = await _resolve_llm(ctx)
    sub_msgs = resumed_msgs if resumed_msgs is not None else _build_initial_msgs(task, sub_ctx, role)
    if resumed_msgs is not None:
        sub_msgs.append({"role": "user", "content": f"新任务：{task}"})

    await events.put({"event": "tool", "tool_name": "subagent", "status": "start",
                      "subagent_id": sub_id, "queue": "subagent",
                      "detail": f"委托子代理 {sub_id}（role={role}，{len(allowed)} 工具，预算 {_settings.subagent_max_rounds} 轮）",
                      "brief": "子代理开始工作"})

    loop_coro = run_subagent_loop(args, sub_ctx, llm, events, sub_id, sub_msgs, schemas, allowed)

    if mode == "background":
        # 真后台化：独立任务立即返回；完成回调写注册表 + progress 事件 + interrupt 通知
        rec = SubagentTask(
            id=sub_id, session_id=ctx.session_id, role=role,
            work_subdir=sub_ctx.work_subdir, task_text=task,
            sub_msgs=sub_msgs, started_at=time.monotonic(),
        )
        rec.task = asyncio.create_task(_background_wrapper(rec, loop_coro, events))
        registry.register(rec)
        await events.put({"event": "progress", "kind": "subagent", "agent": sub_id,
                          "status": "running", "summary": task[:120]})
        return {"subagent_id": sub_id, "status": "running",
                "note": "子代理已在后台启动。可用 subagent(mode=poll|wait_all) 查询/汇合；完成时系统会通知你。"}

    result = await loop_coro
    if resumed_msgs is not None:
        # 续聊收尾：把最新上下文与报告写回注册表（保持可再次续聊）
        prev = registry.get(sub_id)
        if prev is not None:
            prev.sub_msgs = sub_msgs
            prev.report = result.get("report") or ""
            prev.status = "done"
            prev.finished_at = time.monotonic()
    return result


def _work_files_of(ctx: ToolContext) -> list[str]:
    """子代理 work 子目录文件清单（报告可引用）。"""
    from pathlib import Path

    base = Path(_settings.sandbox_dir) / str(ctx.session_id) / str(ctx.round_id) / "work"
    d = base / (ctx.work_subdir or "")
    # F1（红队二次）：目录层 containment 兜底（白名单已拒 ../，此处纵深防御）
    try:
        if not d.resolve().is_relative_to(base.resolve()):
            return []
    except OSError:
        return []
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_file())
