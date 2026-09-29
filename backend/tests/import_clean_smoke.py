"""xhs 互动统计归一回归测试（原 import_clean_smoke，2026-09-04 建；2026-09-17 瘦身）。

原覆盖 import_pipeline.parse_workbook 的导入清洗行为——**数据导入管线已随数据查询线下线删除**。
2026-09-23（去游客态第 4 步）：原测的游客模块 `xhs_gateway._normalize_stats` 随游客态删除，
改测 `xhs_task_service._stat_val`——它是 go 链路写入 xlsx 汇总表前的**同一道归一**
（-1 未知哨兵 → 空、平台原文保留）。

用法：cd backend && source scripts/env_aip.sh && python -u tests/import_clean_smoke.py
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
    from app.services.xhs_task_service import _stat_val

    # ---------- xhs 互动 -1 归一（go 链路的统计值） ----------
    record("xhs 互动 -1 → 空（未知哨兵）", _stat_val(-1) == "", repr(_stat_val(-1)))
    record("xhs 互动 -1.0 → 空", _stat_val(-1.0) == "", repr(_stat_val(-1.0)))
    record("xhs 互动 0 保留", _stat_val(0) == "0", repr(_stat_val(0)))
    record("xhs 互动数字 → 整数字符串", _stat_val(3564) == "3564", repr(_stat_val(3564)))
    record("xhs 纯数字字符串 → 原样", _stat_val("276") == "276", repr(_stat_val("276")))
    record("xhs 平台原文（1.2万）原样保留", _stat_val("1.2万") == "1.2万", repr(_stat_val("1.2万")))
    record("xhs 空值 → 空", _stat_val(None) == "" and _stat_val("") == "", "")

    fails = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS)} 用例，{len(fails)} 失败")
    for r in fails:
        print("  ✗", r[0], r[2])
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
