"""MCP 外部工具的运行时注册（2026-09-28）。

平台既有的 MCP 工具是静态注册；本模块把「MCP 工具」条目改为**启动时动态发现**：
读 `mcp_tools` 表里 status=active 且配置了 url 的条目 → 按 MCP 协议 tools/list →
为每个子工具建 ToolSpec（`group="mcp"`、`hidden=True`——展示入口在 /mcp 页，
注入闸门仍是「运维 active ∧ 员工个人启用」，见 skill_router）。

- 调用转发：handler → McpClient.call_tool（Streamable HTTP）
- 失败降级：条目连不上只记日志跳过，不影响启动与其他工具
- 幂等：每次刷新先摘掉上一轮注册的 mcp 组工具，再按当前条目重建
"""
from __future__ import annotations

from sqlalchemy import text

from app.agent.tools import _REGISTRY, ToolSpec, register_tool
from app.core.database import get_global_engine
from app.core.logging import get_logger

logger = get_logger("agent.tools.mcp_dynamic")

_FETCH_TIMEOUT = 15.0     # 发现阶段（tools/list）短超时——启动不被慢 server 拖住
_CALL_TIMEOUT = 300.0     # 调用阶段：浏览器类工具首次拉起可达数十秒，给足余量


def _handler_factory(url: str, remote: str):
    """生成转发到远端 MCP server 的工具 handler（返回文本；错误转成人话而非异常穿透）。"""
    async def _handler(args: dict, ctx) -> str:  # noqa: ARG001
        from app.services.mcp_client import McpClient, McpError

        client = McpClient(url, timeout=_CALL_TIMEOUT)
        try:
            return await client.call_tool(remote, args or {})
        except McpError as e:
            return f"[MCP 工具报错] {str(e)[:400]}"
        except Exception as e:  # noqa: BLE001 —— 远端不可达/超时统一降级为可读文本
            return f"[MCP 调用失败] {str(e)[:300]}"
        finally:
            await client.close()
    return _handler


async def refresh_mcp_tools() -> int:
    """（重）发现并注册全部 MCP 子工具；返回注册总数。"""
    from app.services.mcp_client import McpClient

    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT id, name, icon, url FROM mcp_tools "
            "WHERE status = 'active' AND url IS NOT NULL AND url <> '' ORDER BY sort_order"
        ))).all()

    # 先摘掉上一轮动态注册的（条目下线/改地址后不残留）
    for name in [n for n, t in list(_REGISTRY.items()) if t.group == "mcp"]:
        _REGISTRY.pop(name, None)

    total = 0
    for row in rows:
        try:
            client = McpClient(row.url, timeout=_FETCH_TIMEOUT)
            try:
                tools = await client.list_tools()
            finally:
                await client.close()
        except Exception as e:  # noqa: BLE001 —— 单个条目不拖垮启动
            logger.warning("MCP 工具发现失败（跳过）%s: %s", row.id, str(e)[:150])
            continue
        n = 0
        for t in tools:
            rname = str(t.get("name") or "").strip()
            if not rname:
                continue
            desc = str(t.get("description") or "").strip()
            first_line = desc.splitlines()[0][:48] if desc else rname
            register_tool(ToolSpec(
                name=rname,
                description=desc or rname,
                parameters=t.get("inputSchema") or {"type": "object", "properties": {}},
                queue="default",
                handler=_handler_factory(row.url, rname),
                display_name=first_line or rname,
                icon=row.icon or "globe",
                group="mcp",
                hidden=True,          # 展示入口在 /mcp 页（一个条目），功能栏不列子工具
                sort_order=90,
                summary=f"{row.name}：{first_line}"[:90],
                user_description=desc or rname,
            ))
            n += 1
            total += 1
        logger.info("MCP 条目 %s（%s）：注册 %d 个子工具", row.id, row.name, n)

    if total:
        logger.info("MCP 工具发现完成：共 %d 个（%d 个条目）", total, len(rows))
    return total
