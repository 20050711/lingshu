"""测试账号密码集中读取（2026-09-10 用户要求：脚本内不再出现明文，也不再随部署同步出去）。

**为什么**：`backend/tests/` 有 35+ 个脚本内嵌平台账号密码（demo/admin/demo1…；含人工走查账号），
部署同步 `tests/` 时把这些明文一起带到了部署机（走查时发现）。

取用顺序：
1. 环境变量 `TEST_PW_<账号大写>`（如 `TEST_PW_MARKET`）——CI/临时覆盖用；
2. `backend/.env.test`（**已 gitignore、不同步部署机**）里的同名键。

缺失**直接抛错**并给出提示：宁可脚本明确报"缺凭证"，也不要静默用空密码跑出一堆 401
（那种形态更难查）。

用法：
    from tests.creds import pw
    await login(c, "demo", "demo", pw("demo"))
"""
from __future__ import annotations

import os
from pathlib import Path

_ENV_TEST = Path(__file__).resolve().parent.parent / ".env.test"
_CACHE: dict[str, str] = {}


def _from_file(key: str) -> str:
    if not _ENV_TEST.is_file():
        return ""
    for line in _ENV_TEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return ""


def pw(account: str) -> str:
    """取测试账号密码（account 用账号名，如 demo / admin / demo1 / test1）。"""
    key = f"TEST_PW_{account.upper()}"
    if key not in _CACHE:
        _CACHE[key] = os.environ.get(key) or _from_file(key)
    val = _CACHE[key]
    if not val:
        raise RuntimeError(
            f"缺少测试账号密码 {key}：写进 backend/.env.test（不入库/不同步）或导出环境变量。"
            f"密码表见 docs/交接文档/HANDOVER.md 与本地记忆。")
    return val
