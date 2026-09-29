"""system_config 缓存隔离冒烟（2026-09-10 走查 bug 回归）。

**现象**：运维给员工开了「示例站点」外部工具（`user_mcp.{uid}=["xhs"]`，DB 里在、时间为证），
员工侧工具就是不出现——agent 拿不到 `xhs`，只能退化成联网搜索。

**根因**：`config_service._CACHE` 里两个互不相干的缓存（`kv`=全量 system_config、
`mcp_active`=mcp_tools 启用集）**共用一个时间戳 `ts`**。skill_router 的顺序是
「先 `get_active_mcp_tool_ids()`（刷新 ts、不填 kv）→ 后 `get_user_mcp_enabled()`（走
`_load_all`）」，后者的"缓存新鲜"判定被前者顶掉 → 直接返回**空 kv** → `user_mcp.{uid}`
永远读成 None（缺省=全禁用）。同一把 ts 还会让 kv 在稳态下**再也不刷新**——
model_layer / dept_tools / user_tools / custom_allow / filelib_perms 全部读到空配置（＝回落默认值）。

用例（自建自删，不碰真实用户；用不存在的 uid 999999）：
1. 冷缓存 + skill_router 真实顺序 → 个人启用仍读得到（修前 None）
2. kv 失效 + mcp_active 新鲜 → `_load_all()` 必须真读 DB（修前返回陈旧值）
3. 反向顺序（先个人后 active）不回归
4. 未配置用户 = 全禁（None）
5. 运维启停立即生效：invalidate_mcp_cache() 后不再等 60s TTL（写路径失效成对覆盖）

用法：cd backend && source scripts/env_aip.sh && python -u tests/config_cache_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.core.database import get_global_engine  # noqa: E402
from app.services import config_service as cs  # noqa: E402

PASS = 0
FAIL = 0
UID = 999999                      # 不存在的用户（get_user_mcp_enabled 不校验存在性）
KEY = f"user_mcp.{UID}"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def cold_cache() -> None:
    """冷缓存（=后端刚重启的状态）。"""
    cs._CACHE["ts"] = 0.0
    cs._CACHE["mcp_ts"] = 0.0
    cs._CACHE["kv"] = {}
    cs._CACHE.pop("mcp_active", None)


async def _put(value: str) -> None:
    async with get_global_engine().begin() as conn:
        await conn.execute(text(
            "INSERT INTO system_config (key, value) VALUES (:k, :v) "
            "ON CONFLICT (key) DO UPDATE SET value=:v"), {"k": KEY, "v": value})


async def _del() -> None:
    async with get_global_engine().begin() as conn:
        await conn.execute(text("DELETE FROM system_config WHERE key=:k"), {"k": KEY})


async def _put_mcp(tid: str, status: str) -> None:
    """临时 mcp_tools 行（自建自删，不碰真实工具行）。"""
    async with get_global_engine().begin() as conn:
        await conn.execute(text(
            "INSERT INTO mcp_tools (id, name, status, sort_order) VALUES (:i, :i, :s, 999) "
            "ON CONFLICT (id) DO UPDATE SET status=:s"), {"i": tid, "s": status})


async def _del_mcp(tid: str) -> None:
    async with get_global_engine().begin() as conn:
        await conn.execute(text("DELETE FROM mcp_tools WHERE id=:i"), {"i": tid})


async def main() -> int:
    try:
        # 1. 冷缓存 + skill_router 真实调用顺序
        await _put(json.dumps(["xhs"]))
        cold_cache()
        await cs.get_dept_tools("demo")
        await cs.get_user_tools(UID)
        await cs.get_active_mcp_tool_ids()
        mine = await cs.get_user_mcp_enabled(UID)
        check("router 顺序下个人启用可读（修前为 None）", mine == ["xhs"], f"{mine!r}")

        # 2. kv 失效 + mcp_active 新鲜 → _load_all 必须真读 DB（防"稳态永不刷新"）
        cold_cache()
        await _put(json.dumps(["xhs"]))
        check("先填充 kv", (await cs._load_all()).get(KEY) == json.dumps(["xhs"]))
        await _put(json.dumps(["xhs", "web_fetch"]))     # 改配置（直写 DB）
        cs._CACHE["ts"] = 0.0                            # 模拟写入方失效 kv（真实 setter 即如此）
        await cs.get_active_mcp_tool_ids()               # 只该刷新 mcp_ts
        got = (await cs._load_all()).get(KEY)
        check("kv 失效后不被 mcp 时间戳顶掉（修前读到陈旧值）",
              got == json.dumps(["xhs", "web_fetch"]), f"{got!r}")

        # 3. 反向顺序不回归
        cold_cache()
        mine2 = await cs.get_user_mcp_enabled(UID)
        check("反向顺序仍正常", mine2 == ["xhs", "web_fetch"], f"{mine2!r}")

        # 4. 缺省=全禁（未配置用户）
        cold_cache()
        check("未配置用户 = 全禁（None）", await cs.get_user_mcp_enabled(999998) is None)

        # 5. 运维启停立即生效：invalidate_mcp_cache() 后不应再吃 60s 旧缓存
        #    （2026-09-10 同类排查：mcp_tools.status 的写路径原先一处失效都没做）
        tid = "smoke_tmp_tool"
        await _put_mcp(tid, "active")
        cold_cache()
        check("临时工具 active 进入启用集", tid in await cs.get_active_mcp_tool_ids())
        await _put_mcp(tid, "disabled")
        check("停用后不失效 → 仍读到旧值（证明缓存确实在生效）",
              tid in await cs.get_active_mcp_tool_ids())
        cs.invalidate_mcp_cache()
        check("invalidate_mcp_cache() 后立即生效（无需等 TTL）",
              tid not in await cs.get_active_mcp_tool_ids())
    finally:
        await _del()
        await _del_mcp("smoke_tmp_tool")
        cold_cache()
    print(f"=== {'PASS' if FAIL == 0 else 'FAIL'}（{PASS} 过 / {FAIL} 败）===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
