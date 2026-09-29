"""JSON 兜底：把非原生类型转成可序列化形态。

2026-09-24 事故（走查实报）：某工具返回的金额是 NUMERIC 列读回的 `Decimal`，
`tool_exec._result_to_text` 的 `json.dumps` 没有 `default=` → `Object of type Decimal is not
JSON serializable` → 异常穿透到 graph → 用户看到「Agent 执行异常」而整轮报废。
**一个非 JSON 原生值就能炸掉一整轮**，代价远大于"把值降级成字符串"——
所以这里是兜底而非白名单：认识常见类型（Decimal/时间/UUID/bytes/set），其余一律 `str()`。

用法：`json.dumps(obj, ensure_ascii=False, default=json_default)`。
（数据源头该归一的地方仍应归一——各服务把行对象转 dict 时自行归一；这里只保证"别再炸"。）
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import Any
from uuid import UUID


def json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, _dt.datetime):
        return o.isoformat(sep=" ", timespec="seconds")
    if isinstance(o, (_dt.date, _dt.time)):
        return o.isoformat()
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, (bytes, bytearray, memoryview)):
        return bytes(o).decode("utf-8", "replace")
    if isinstance(o, (set, frozenset)):
        return list(o)
    return str(o)
