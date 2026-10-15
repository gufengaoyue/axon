"""MCP 连接器（mcp>=2 适配；设计 §4.2/需求文档沙箱 #5）。

v2 一等公民 Client（2026-10-13 官方文档核实）：
    from mcp import Client
    async with Client(url | StdioServerParameters(...)) as client: ...
    await client.call_tool(name, args) -> result.structured_content

本适配器把每个 MCP 工具包装成 ToolSpec：
  - 披露名 mcp__<server>__<tool>（避免与内置工具撞名）
  - 每次调用 = Action(kind="mcp") 过沙箱网关（整体/单独开关在 config.mcp.servers）
  - 异步 Client 跑在专用后台线程的事件循环里，同步 handler 经
    run_coroutine_threadsafe 桥接
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from axon.sandbox.gateway import Action
from axon.sandbox.risk import RiskClass
from axon.tools.registry import ToolResult, ToolSpec, tool_result_err, tool_result_ok


class MCPUnavailable(RuntimeError):
    pass


class MCPConnector:
    def __init__(self, cfg, gateway):
        self.cfg = cfg
        self.gateway = gateway
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client = None
        self._client_cm = None
        self._tools: list[dict] = []

    # ---- 生命周期 ----
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
            self._thread.start()
        return self._loop

    def connect(self) -> None:
        try:
            from mcp import Client  # noqa: F401
        except ImportError as e:
            raise MCPUnavailable("未安装 mcp 包（pip install -e .[mcp]）") from e
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(self._aconnect(), loop)
        fut.result(timeout=30)

    async def _aconnect(self) -> None:
        from mcp import Client
        if self.cfg.transport == "http":
            target = self.cfg.url
        else:
            try:
                from mcp import StdioServerParameters
            except ImportError:
                from mcp.client.stdio import StdioServerParameters
            target = StdioServerParameters(command=self.cfg.command or "",
                                           args=self.cfg.args or [])
        self._client_cm = Client(target)   # async with 进入即协商协议版本
        self._client = await self._client_cm.__aenter__()
        tools = await self._client.list_tools()
        self._tools = [{"name": t.name, "description": t.description or "",
                        "inputSchema": t.inputSchema}
                       for t in tools.tools]

    def close(self) -> None:
        if self._client_cm is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._client_cm.__aexit__(None, None, None),
                                             self._loop).result(timeout=10)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ---- 工具包装 ----
    def tool_specs(self) -> list[ToolSpec]:
        specs = []
        enabled_map = self.cfg.tools or {}
        for t in self._tools:
            if enabled_map.get(t["name"]) is False:
                continue  # 单独禁用
            specs.append(self._wrap(t))
        return specs

    def _wrap(self, t: dict) -> ToolSpec:
        full_name = f"mcp__{self.cfg.name}__{t['name']}"

        def handler(call, args: dict, gw) -> ToolResult:
            if self._client is None:
                return tool_result_err(call, "permanent", "MCP 连接未建立")
            loop = self._ensure_loop()
            fut = asyncio.run_coroutine_threadsafe(
                self._client.call_tool(t["name"], args), loop)
            try:
                result = fut.result(timeout=60)
            except Exception as e:
                return tool_result_err(call, "transient", f"MCP 调用失败: {e}")
            content = getattr(result, "structured_content", None)
            if content is None:
                parts = [getattr(c, "text", str(c)) for c in getattr(result, "content", [])]
                content = "\n".join(parts)
            import json
            return tool_result_ok(call, json.dumps(content, ensure_ascii=False, default=str),
                                  meta={"mcp_tool": t["name"], "server": self.cfg.name})

        def to_action(args: dict) -> Action:
            return Action(kind="mcp", target=full_name, detail={"tool": t["name"], **args})

        return ToolSpec(
            name=full_name,
            description=f"[MCP:{self.cfg.name}] {t['description']}",
            parameters=t.get("inputSchema") or {"type": "object", "properties": {}},
            handler=handler,
            to_action=to_action,
            risk=RiskClass.MCP,
            mutating=False,
            parallel_safe=False,
            timeout=60.0,
        )
