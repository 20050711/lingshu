"""工具注册与沙盒冒烟测试（2026-08-21 修订：内置技能已下线——删 skill 全链路/内部令牌段）。

用法: python tests/skill_smoke.py
覆盖：
1. sandbox_exec 三语言 hello（skillenv python / bash / node）
2. 工具注册：21 个（3 内置技能下线后），user_description 存在
3. 超时 killpg（skillenv python while True）
4. select_mode 守卫：无 single 工具（前端单选入口已下线）+ xhs 必为 mcp/multi
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.tools import ToolContext, get_all_tools
from app.core.config import get_settings
from app.services import sandbox_exec

CTX = ToolContext("smoke-skill", 1, "demo", "employee", "smoke", "/data/outputs/smoke/1", user_id=1)
_settings = get_settings()


async def main() -> int:
    results = []

    # 1. sandbox_exec 三语言（skillenv python 完整 site-packages / bash / node）
    #    node 需 5GB 虚拟内存（V8 pointer compression cage）+ nproc 1024（WSL2 计数膨胀）
    for lang, code, expect, kw in [
        ("python", "import markitdown; print('py-ok')", "py-ok", {}),
        ("bash", "echo bash-ok", "bash-ok", {}),
        ("node", "console.log('node-ok')", "node-ok", {"memory_mb": 5120}),
    ]:
        r = await sandbox_exec.run(
            lang=lang, code=code, cwd="/tmp",
            env_extra={"PATH": f"{Path(_settings.skillenv_python).parent}:{Path(_settings.node_path).parent}:/usr/bin:/bin"},
            timeout_s=20, **kw,
            interpreter=_settings.skillenv_python if lang == "python" else None,
        )
        ok = expect in (r.get("stdout") or "") and r.get("exit_code") == 0
        results.append((f"sandbox_exec {lang}", ok, (r.get("stdout") or r.get("error") or "")[:60]))

    # 2. 注册与元数据（2026-08-21：内置三技能（framework/officecli/ppt_master）已下线 24→21；
    # 2026-08-26：新增 skill_read（Q8 技能按需加载）21→22；
    # 2026-09-03：xhs_download MCP 工具 25→26；2026-09-10：kb_match/kb_read 下线 + file_search 上线 → 26-2+1=25)
    tools = get_all_tools()
    names = [t.name for t in tools]
    # 2026-09-15：新增 video_understand/audio_transcribe（智能助手媒体工具）25→27
    # 本副本按裁剪后代码重建：工具数以下方集合为准——
    #             当天回归清单里没有 skill_smoke，这条计数断言漏更（本次走查才发现）
    # 2026-09-17（下半天）：数据查询线下线，sql_query 删除 30→29
    # 2026-09-22：feishu_files（"导哪份资料"的入口）上线，29→30

    ok = len(tools) == 33
    results.append(("注册工具数（本副本按裁剪后代码重建，含 skill_read + 媒体工具两件套）", ok, str(names)))
    ok = all(t.user_description for t in tools)
    results.append(("全部工具 user_description", ok, ""))
    # 2026-09-24（走查：时间线过程/结束不许出现英语）——两条结构性守卫：
    # ① 每个工具都要有中文 display_name（缺了就回落成英文 id，前端时间线会露英文）；
    # ② 每个并发队列都要能译成中文（tool_exec 的"队列 X，立即执行"用它渲染）。
    from app.agent.tools import QUEUE_LABELS, tool_label
    no_cn = sorted(t.name for t in tools if tool_label(t.name) == t.name)
    results.append(("全部工具有中文展示名（时间线不露英文 id）", not no_cn, str(no_cn)))
    missing_q = sorted({t.queue for t in tools} - set(QUEUE_LABELS))
    results.append(("全部并发队列有中文名（时间线'队列 X'不露英文）", not missing_q, str(missing_q)))
    ok = "framework" not in names and "officecli" not in names and "ppt_master" not in names
    results.append(("内置三技能已移除", ok, ""))
    # 4. select_mode 守卫（2026-09-10 走查 bug）：前端技能浮窗 `filter(s => s.select_mode !== 'single')`
    # **把 single 工具整个过滤掉**（单选机制 2026-08-21 已下线），而 auto 模式只放行"用户显式勾选的
    # single"——于是任何 single 工具＝**UI 无入口 + 永不注入**（示例站点 xhs 当天即因此消失）。
    # 谁再改成 single，这里先炸。
    singles = [t.name for t in tools if t.select_mode == "single"]
    results.append(("无 select_mode=single 工具（前端单选入口已下线，否则工具永不可见）",
                    not singles, str(singles)))
    xhs_tool = next((t for t in tools if t.name == "xhs"), None)
    results.append(("xhs 为 group=mcp 且 select_mode=multi（闸门=运维active∧员工启用）",
                    xhs_tool is not None and xhs_tool.group == "mcp" and xhs_tool.select_mode == "multi",
                    f"{getattr(xhs_tool, 'group', None)}/{getattr(xhs_tool, 'select_mode', None)}"))

    # 3. 超时 killpg（skillenv python）
    r = await sandbox_exec.run(lang="python", code="while True: pass", cwd="/tmp",
                               timeout_s=5, memory_mb=512, interpreter=_settings.skillenv_python)
    ok = "超时" in (r.get("error") or "")
    results.append(("sandbox 超时 killpg", ok, (r.get("error") or "")[:50]))

    # 4. MCP 外部工具真的注入（2026-09-17 走查 bug：/mcp 勾了工具，模型仍答"没有挂载"）
    #    根因：工具 hidden=True → 不在 /skills 功能栏 → `resolved`（用户勾选集合）里永远没有它；
    #    而 MCP 闸门**只过滤不添加** → 任何模式都注入不了。守卫：勾选后必须出现在 allowed_tools。
    from sqlalchemy import text

    from app.agent.nodes.skill_router import run_skill_router
    from app.agent.tools import get_mcp_tool_names
    from app.core.database import get_global_engine
    from app.services.config_service import get_user_mcp_enabled, set_user_mcp_enabled

    async with get_global_engine().connect() as conn:
        uid = (await conn.execute(text(
            "SELECT id FROM users WHERE username='demo_admin'"))).scalar()
        dept = (await conn.execute(text(
            "SELECT department_id FROM users WHERE id=:u"), {"u": uid})).scalar()
    before = await get_user_mcp_enabled(uid)
    state = {"user_id": uid, "user_role": "dept_admin", "department_id": dept,
             "active_skills": [], "auto_skill": False, "mode": "quick",
             "session_id": "smoke-skill", "messages": []}
    try:
        await set_user_mcp_enabled(uid, ["feishu"])
        got = [t for t in (await run_skill_router(state, {}))["allowed_tools"] if t.startswith("feishu")]
        want = sorted(t for t in get_mcp_tool_names() if t.startswith("feishu"))
        results.append(("MCP 勾选后工具真的进 allowed_tools（hidden 工具靠闸门并入）",
                        got == want, f"得到 {got} / 期望 {want}"))
        await set_user_mcp_enabled(uid, [])
        got2 = [t for t in (await run_skill_router(state, {}))["allowed_tools"] if t.startswith("feishu")]
        results.append(("MCP 未勾选时一律不注入（默认关）", not got2, str(got2)))
    finally:                                        # 还原（别把测试账号的勾选弄脏）
        await set_user_mcp_enabled(uid, before or [])

    for name, ok, info in results:
        print(f"  {'✓' if ok else '✗'} {name}: {info}")
    all_ok = all(ok for _, ok, _ in results)
    print(f"\nskill_smoke: {'全部通过' if all_ok else f'{sum(1 for _, ok, _ in results if not ok)} 项失败'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
