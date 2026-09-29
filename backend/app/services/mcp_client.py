"""轻量 MCP（Model Context Protocol）Streamable-HTTP 客户端（2026-09-03）。

零第三方依赖（仅 httpx，平台已有）：直接走 JSON-RPC 2.0 over HTTP，
兼容官方/fastmcp 等标准 MCP server（:5556/mcp/ 之类）。平台侧不引入 mcp 官方
SDK——避免大版本（mcp 1.x/2.x）与远端 server 锁版不匹配；本实现对 2024-11-05
与 2025-03-26 协议的 server 均可用（initialize 协议版本协商由 server 回退）。

用法：
    client = McpClient("http://127.0.0.1:5556/mcp/", timeout=60)
    tools = await client.list_tools()
    text = await client.call_tool("get_detail_data", {"url": "..."})
"""
from __future__ import annotations

import json

import httpx

MCP_ACCEPT = "application/json, text/event-stream"


class McpError(Exception):
    """MCP 调用错误（JSON-RPC error 或远端返回 isError）。"""


class McpClient:
    def __init__(self, url: str, timeout: float = 30.0, auth_token: str | None = None):
        # fastmcp 的 Streamable HTTP 挂在无尾斜杠路径（/mcp 而非 /mcp/，带斜杠会 307）
        self.url = url.rstrip("/")
        # 2026-09-09：可选 Bearer 鉴权（外部 MCP server 用 AUTH_TOKEN，与平台
        # login 无头访客 MCP 不同源）；None=保持旧行为（无 Authorization 头，向后兼容）
        self._auth = auth_token
        self._client = httpx.AsyncClient(timeout=timeout)
        self._initialized = False
        self._session_id: str | None = None
        self._rpc_id = 0

    async def _post(self, payload: dict) -> dict:
        headers = {"Accept": MCP_ACCEPT, "Content-Type": "application/json"}
        if self._auth:
            headers["Authorization"] = f"Bearer {self._auth}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        resp = await self._client.post(self.url, json=payload, headers=headers)
        if not self._session_id:
            self._session_id = resp.headers.get("mcp-session-id")
        resp.raise_for_status()
        if not resp.content:
            return {}  # 通知类（202/204 空 body）
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            # fastmcp 一律 SSE 响应：取最后一个 data: JSON 帧（event: message 行忽略）
            data = None
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    data = line[5:].strip()
            if not data:
                raise McpError(f"MCP SSE 响应无 data 帧: {resp.text[:200]}")
            return json.loads(data)
        return resp.json()

    async def _request(self, method: str, params: dict | None = None) -> dict:
        self._rpc_id += 1
        payload: dict = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method}
        if params is not None:
            payload["params"] = params
        body = await self._post(payload)
        if "error" in body:
            err = body["error"]
            raise McpError(f"MCP {method} 错误: {err.get('message') or err}")
        return body.get("result") or {}

    async def _ensure_init(self) -> None:
        if self._initialized:
            return
        result = await self._request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "lingshu-mcp-bridge", "version": "1.0"},
        })
        # notifications/initialized 为通知（无 id）；失败不阻断（部分 server 不要求）
        try:
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except httpx.HTTPError:
            pass
        self._initialized = True
        return result

    async def ping(self) -> dict:
        """连接测试（initialize 握手），返回 serverInfo。"""
        return await self._ensure_init()

    async def list_tools(self) -> list[dict]:
        await self._ensure_init()
        result = await self._request("tools/list")
        return result.get("tools") or []

    async def call_tool(self, name: str, args: dict) -> str:
        """调用 MCP 工具，返回全部 text 内容拼接；远端报错(isError)抛 McpError。"""
        await self._ensure_init()
        result = await self._request("tools/call", {"name": name, "arguments": args})
        if result.get("isError"):
            texts = [str(c.get("text") or c.get("content") or "") for c in result.get("content") or []]
            raise McpError("MCP 远端错误: " + ("".join(texts) or str(result)[:300]))
        parts = []
        for c in result.get("content") or []:
            if c.get("type") == "text" and c.get("text"):
                parts.append(c["text"])
            elif c.get("type") == "resource" and c.get("resource"):
                parts.append(str(c["resource"]))
            elif c.get("type") == "image" and c.get("data"):
                # 2026-09-11（走查：换号扫不了码，接口 400"未取得登录二维码"）：MCP 的 image 内容块
                # 原先被**静默丢弃**——go 端取登录二维码返回的就是 {type:image, mimeType:image/png,
                # data:<base64>}，text 里只有一句扫码提示，网关的 iVBOR 正则永远匹配不到。
                # 统一转成 data URI 拼进文本（图片消费方按 data:image/...;base64, 前缀解析；纯文本调用方不受影响）。
                parts.append(f"data:{c.get('mimeType') or 'image/png'};base64,{c['data']}")
        return "\n".join(parts)

    async def close(self) -> None:
        await self._client.aclose()
