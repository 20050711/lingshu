"""工具结果 JSON 兜底回归（纯单测，不碰 DB/网络；2026-09-24）。

背景（走查实报）：`pgy_kol_search` 返回的报价是 NUMERIC 读回的 `Decimal`，
`tool_exec._result_to_text` 的 json.dumps 没有 `default=` → 抛 TypeError → 穿透到 graph →
用户看到「Agent 执行异常: Object of type Decimal is not JSON serializable」而**整轮报废**。
修法：`app/core/json_safe.py` 的 `json_default` 兜底 + tool_exec/subagent 所有 dumps 带上它。

用法：cd backend && source scripts/env_aip.sh && python -u tests/json_safe_smoke.py
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))


# 一份"什么都有一点"的工具结果（覆盖实际会遇到的非原生类型）
UGLY = {
    "quote": {"picture": Decimal("2200.00"), "video": Decimal("2800.50")},
    "created_at": datetime(2026, 9, 24, 15, 28, 19),
    "day": date(2026, 9, 24),
    "task_id": uuid4(),
    "blob": b"\xe4\xbd\xa0\xe5\xa5\xbd",
    "tags": {"美妆", "美食"},
    "nested": [{"price": Decimal("50")}],
}


def main() -> None:
    from app.core.json_safe import json_default

    # ---------- 1. json_default 的类型映射 ----------
    record("Decimal → float（报价不能被降级成字符串）",
           json_default(Decimal("2200.00")) == 2200.0)
    record("datetime → 'YYYY-MM-DD HH:MM:SS'",
           json_default(datetime(2026, 9, 24, 15, 28, 19)) == "2026-09-24 15:28:19")
    record("date → ISO", json_default(date(2026, 9, 24)) == "2026-09-24")
    u = uuid4()
    record("UUID → str", json_default(u) == str(u))
    record("bytes → 解码后的字符串", json_default(b"\xe4\xbd\xa0\xe5\xa5\xbd") == "你好")
    record("set → list", sorted(json_default({"b", "a"})) == ["a", "b"])
    record("未知类型 → str（兜底：宁可降级也不炸）",
           json_default(object()).startswith("<object object at"))

    # ---------- 2. 三个回填函数：不抛错 + 仍是合法 JSON ----------
    from app.agent.nodes.tool_exec import _result_to_text, _result_to_text_degraded
    from app.agent.subagent import _result_to_text as _sub_result_to_text

    for label, fn, args in (
        ("tool_exec._result_to_text", _result_to_text, ()),
        ("tool_exec._result_to_text_degraded", _result_to_text_degraded, ()),
        ("subagent._result_to_text", _sub_result_to_text, (4000,)),
    ):
        try:
            text = fn(UGLY, *args)
            parsed = json.loads(text)                      # D4 教训：必须仍是合法 JSON
            ok = parsed["quote"]["picture"] == 2200.0 and isinstance(parsed["task_id"], str)
            record(f"{label}：含 Decimal/时间/UUID 也能出合法 JSON", ok, f"{len(text)} 字符")
        except Exception as e:                             # noqa: BLE001 —— 这里就是要抓任意异常
            record(f"{label}：含 Decimal/时间/UUID 也能出合法 JSON", False, f"{type(e).__name__}: {e}")

    # ---------- 3. 回归现场：原来必炸的那一种 ----------
    try:
        text = _result_to_text({"quote": {"picture": Decimal("6000.00")}})
        record("原事故形态（只有报价是 Decimal）不再抛错",
               json.loads(text)["quote"]["picture"] == 6000.0)
    except Exception as e:                                 # noqa: BLE001
        record("原事故形态（只有报价是 Decimal）不再抛错", False, f"{type(e).__name__}: {e}")

    print("\n" + "=" * 40)
    failed = [r for r in RESULTS if not r[1]]
    print(f"总计 {len(RESULTS)} 项，失败 {len(failed)} 项")
    for name, _, detail in failed:
        print(f"  ✗ {name} {detail}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
