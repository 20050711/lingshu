"""数值列统计共用函数（4.1 抽取）：file_parse 与知识库/客户索引的分块共用。

统计放明细前返回——即使回填 LLM 被截断（tool_result_max_chars），统计仍在（HANDOVER 踩坑 26 精神）。
"""
from __future__ import annotations


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except (ValueError, TypeError):
        return None


def col_stats(headers: list[str], rows: list[list]) -> dict:
    """数值列统计（sum/avg/max/min），仅统计可转 float 的列。"""
    stats: dict[str, dict] = {}
    for i, h in enumerate(headers):
        vals: list[float] = []
        for r in rows:
            if i < len(r):
                v = _num(r[i])
                if v is not None:
                    vals.append(v)
        if vals:
            stats[h] = {
                "sum": round(sum(vals), 2),
                "avg": round(sum(vals) / len(vals), 2),
                "max": max(vals),
                "min": min(vals),
            }
    return stats
