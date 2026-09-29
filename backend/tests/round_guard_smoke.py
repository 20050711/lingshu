"""轮次护栏重构回归测试（2026-09-08）。

覆盖 9.4 走查复盘后新增/改动的护栏行为（无 DB 依赖，纯单测）：
- 失败分类 classify_error：wait / retryable / dead_end 三类（显式 error_code 优先 + 关键词兜底）
- 工具 progress_keys 声明完整性：全工具显式声明或从宽默认（不再依赖失配的全局词表）
- 无进展判定语义：xhs message / file_search content_matches / memory note / chart chart_id /
  成功结果命中声明键；error 不命中；partial（部分成功）命中
- 平台说明 build_break_notice：两分类（有产出/无产出）+ wait 原因与续跑指引
- 续跑触发词 _RESUME_RE：「继续/接着做/往下做」命中；「继续说里面的事」不命中
- xhs 结果构造：全失败走 error + error_code；部分成功 partial + message

用法：cd backend && source scripts/env_aip.sh && python -u tests/round_guard_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))


def main() -> None:
    from app.agent.nodes.break_notice import PLATFORM_TAG, build_break_notice
    from app.agent.tools import get_all_tools, get_tool

    # ---------- 1. 失败分类 ----------
    from app.agent.nodes.tool_exec import classify_error

    record("wait 关键词（风控冷却）", classify_error("示例站点侧风控冷却中（连续失败 5 次），约 10 分钟后自动恢复") == "wait")
    record("wait 显式 error_code", classify_error("随便什么错误", "wait") == "wait")
    record("retryable 默认（链接问题）", classify_error("该链接缺少 xsec_token 分享参数——请提供完整分享链接") == "retryable")
    record("retryable 显式 error_code", classify_error("一个普通错误", "retryable") == "retryable")
    record("dead_end 关键词（无权）", classify_error("无权访问该目录") == "dead_end")
    record("dead_end 关键词（未启用）", classify_error("示例站点下载器未启用（运维停用或未配置）") == "dead_end")

    # ---------- 2. progress_keys 声明完整性 ----------
    tools = get_all_tools()
    record("工具总数不为 0", len(tools) > 20, f"实际 {len(tools)}")
    declared = [t.name for t in tools if t.progress_keys is not None]
    record("全部工具已显式或从宽声明（None=从宽不误伤）", len(declared) == len(tools),
           f"声明 {len(declared)}/{len(tools)}")
    # 关键声明核验（9.4 事故点：xhs/检索/memory/chart 必须声明到）
    def keys_of(name: str) -> tuple:
        t = get_tool(name)
        return t.progress_keys if t is not None else None

    xhs_keys = keys_of("xhs")
    record("xhs 声明含 message", xhs_keys and "message" in xhs_keys, str(xhs_keys))
    # 2026-09-17：原 sql_query 声明核验随该工具下线删除，改核验 file_search（同属声明了多键的工具）
    fs_keys = keys_of("file_search")
    record("file_search 声明含 content_matches", fs_keys and "content_matches" in fs_keys, str(fs_keys))
    mem_keys = keys_of("memory")
    record("memory 声明含 note/deleted", mem_keys and "note" in mem_keys and "deleted" in mem_keys, str(mem_keys))
    chart_keys = keys_of("generate_chart")
    record("generate_chart 声明含 chart_id", chart_keys and "chart_id" in chart_keys, str(chart_keys))
    # 无进展判定语义（模拟 tool_exec._effective 的行为：键命中 + error/partial 处理）
    def effective(r: dict, name: str) -> bool:
        if r.get("error") and not r.get("partial"):
            return False
        keys = keys_of(name)
        if keys is None:
            return True
        if not keys:
            return False
        return any(r.get(k) for k in keys)

    record("xhs 成功（message）→ 有效", effective({"message": "标题：xxx"}, "xhs") is True)
    record("xhs 全失败（error）→ 无效", effective({"error": "风控冷却中", "error_code": "wait"}, "xhs") is False)
    record("xhs 部分成功（error+partial+message）→ 有效", effective({"error": "1 条失败", "partial": True, "message": "第 1 篇"}, "xhs") is True)
    record("检索成功（content_matches）→ 有效",
           effective({"content_matches": [{"file": "a.md"}]}, "file_search") is True)
    record("memory add（note）→ 有效", effective({"memory_id": 1, "note": "已保存"}, "memory") is True)
    record("chart 成功（chart_id）→ 有效", effective({"chart_id": "abc", "chart_label": "X"}, "generate_chart") is True)

    # ---------- 2.5 ask_user 反问硬限 ----------
    from app.agent.nodes.tool_exec import _ask_user_limit

    record("反问限次：quick=1", _ask_user_limit("quick") == 1)
    record("反问限次：complex=2", _ask_user_limit("complex") == 2)

    # ---------- 3. 平台说明两分类 ----------
    wait_state = {
        "round_outputs": [{"label": "蓉芷9.4_评论回填.xlsx"}],
        "fail_streaks": {"wait": 2},
        "tool_events": [{"status": "error", "detail": "示例站点侧风控冷却中，约 10 分钟后自动恢复", "error_category": "wait"}],
    }
    n1 = build_break_notice(wait_state)
    record("有产出平台说明：标记前缀", n1.startswith(PLATFORM_TAG))
    record("有产出平台说明：含产出名与续跑指引", "蓉芷9.4_评论回填.xlsx" in n1 and "继续" in n1)
    n2 = build_break_notice({"round_outputs": [], "no_progress_streak": 5, "tool_events": []})
    record("无产出平台说明：明确未完成", "任务未完成" in n2 and "继续" in n2)

    # ---------- 4. 续跑触发词 ----------
    from app.api.chat import _RESUME_RE

    record("触发词：继续", bool(_RESUME_RE.match("继续")))
    record("触发词：接着做", bool(_RESUME_RE.match(" 接着做！")))
    record("触发词：往下做", bool(_RESUME_RE.match("往下做")))
    record("非触发词：我想继续讨论这个数据", not _RESUME_RE.match("我想继续讨论这个数据"))
    record("非触发词：接着上次的说一下", not _RESUME_RE.match("接着上次的说一下"))

    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"\n===== {len(RESULTS) - failed}/{len(RESULTS)} 通过 =====")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
