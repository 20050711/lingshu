"""支柱 3 单测（2026-08-10）：任务清单解析/进度回显 + 工具结果累积降级护栏（合法 JSON）。

用法: cd backend && source scripts/env_aip.sh && python -u tests/test_checklist_guardrail.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def main() -> int:
    from app.agent.nodes.tool_exec import _result_to_text_degraded
    from app.agent.plan_tasks import parse_plan_tasks, task_progress

    # 1. parse_plan_tasks：编号 / 无编号兜底 / 空
    check("编号解析", parse_plan_tasks("1. 解析「渠道数据.xlsx」\n2. 生成同款 HTML 报告") == ["解析「渠道数据.xlsx」", "生成同款 HTML 报告"], "")
    cjk = parse_plan_tasks("一、先查数据\n二、再画图")
    check("中文编号整体 1 条", len(cjk) == 1 and "先查数据" in cjk[0] and "再画图" in cjk[0], str(cjk))
    check("空计划", parse_plan_tasks("") == [], "")

    # 2. task_progress：启发式命中 / 未匹配退化
    tasks = ["解析「渠道数据.xlsx」", "生成同款 HTML 报告"]
    p0 = task_progress(tasks, [], [])
    check("未匹配退化 0/N", "0/2" in p0, p0)
    p1 = task_progress(tasks, [{"type": "doc", "label": "渠道数据.xlsx"}],
                       [{"brief": "已解析文件「渠道数据.xlsx」"}])
    check("引号文件名命中", "1/2" in p1 and "当前：2" in p1, p1)
    check("空清单", task_progress([], [], []) == "", "")

    # 3. 降级护栏：合法 JSON + meta 完整 + stdout 尾保留 + 标记
    degraded = _result_to_text_degraded({
        "stdout": "A" * 3000, "stderr": "B" * 3000, "exit_code": 1,
        "work_files": [{"name": "x.txt", "size": 1}], "duration_s": 0.5,
    })
    d = json.loads(degraded)  # 必须合法 JSON（D4 v1 拼接 bug 教训）
    check("降级合法 JSON", isinstance(d, dict), degraded[:60])
    check("降级保 meta", d.get("exit_code") == 1 and d.get("duration_s") == 0.5, "")
    check("降级 stdout 保尾", d.get("stdout", "").endswith("AAA") and "降级回填尾部" in d.get("stdout", ""), "")
    check("降级护栏标记", "总量护栏" in d.get("note", ""), d.get("note", "")[:40])

    # 4. 非 dict 兜底不崩
    check("非 dict 降级", _result_to_text_degraded("x" * 5000)[:20] == "x" * 20, "")

    print(f"=== {'PASS' if FAIL == 0 else 'FAIL'}（{PASS} 过 / {FAIL} 败）===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
