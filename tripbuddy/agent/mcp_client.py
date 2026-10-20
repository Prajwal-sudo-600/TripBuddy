"""
MCP Client for TripBuddy Agent.
Connects to MCP_URL using the MCP Python SDK client over streamable HTTP.
Never caches tool lists so dynamic tool descriptions are always up-to-date.
"""

import asyncio
import concurrent.futures
import json
from typing import Any, Dict, List, Optional
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool as MCPTool

from .config import get_config


def _run_async(coro):
    """Safely execute an async coroutine from synchronous code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def parse_call_tool_result(result: Any) -> Any:
    """
    Extract exact tool result data from CallToolResult.
    Passes data exactly as returned without filtering or summarization.
    """
    if result is None:
        return None

    if not hasattr(result, "content") or not result.content:
        return ""

    if getattr(result, "isError", False):
        texts = [getattr(c, "text", str(c)) for c in result.content]
        return "\n".join(texts)

    items = []
    for c in result.content:
        text = getattr(c, "text", None)
        if text is not None:
            try:
                items.append(json.loads(text))
            except Exception:
                items.append(text)
        else:
            items.append(str(c))

    if len(items) == 1:
        return items[0]
    return items


class MCPClient:
    """
    Client for interacting with the MCP server over streamable HTTP.
    Can be used as an async context manager or standalone.
    """

    def __init__(self, mcp_url: Optional[str] = None):
        cfg = get_config()
        url = mcp_url or cfg.mcp_url
        if url.startswith("http://localhost:"):
            url = url.replace("http://localhost:", "http://127.0.0.1:", 1)
        self.mcp_url = url
        self._cm_transport = None
        self._cm_session = None
        self._session: Optional[ClientSession] = None


    async def connect(self) -> ClientSession:
        """Connect to the MCP server and initialize the session."""
        if self._session is not None:
            return self._session

        self._cm_transport = streamable_http_client(self.mcp_url)
        read_stream, write_stream, _ = await self._cm_transport.__aenter__()

        self._cm_session = ClientSession(read_stream, write_stream)
        self._session = await self._cm_session.__aenter__()
        await self._session.initialize()
        return self._session

    async def close(self) -> None:
        """Close session and transport."""
        if self._cm_session is not None:
            try:
                await self._cm_session.__aexit__(None, None, None)
            except Exception:
                pass
            self._cm_session = None

        if self._cm_transport is not None:
            try:
                await self._cm_transport.__aexit__(None, None, None)
            except Exception:
                pass
            self._cm_transport = None

        self._session = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def list_tools(self) -> List[MCPTool]:
        """
        Fetch available tools from the MCP server.
        NEVER caches tools to ensure dynamic descriptions are fresh.
        """
        needs_disconnect = False
        if self._session is None:
            await self.connect()
            needs_disconnect = True

        try:
            result = await self._session.list_tools()
            return result.tools
        finally:
            if needs_disconnect:
                await self.close()

    async def call_tool(self, name: str, args: Optional[Dict[str, Any]] = None) -> Any:
        """
        Execute a tool by name with arguments.
        Returns exact tool result data without modification.
        """
        needs_disconnect = False
        if self._session is None:
            await self.connect()
            needs_disconnect = True

        try:
            raw_result = await self._session.call_tool(name=name, arguments=args or {})
            return parse_call_tool_result(raw_result)
        finally:
            if needs_disconnect:
                await self.close()


def list_tools(mcp_url: Optional[str] = None) -> List[MCPTool]:
    """Synchronous helper to list tools from MCP server without caching."""
    async def _action():
        client = MCPClient(mcp_url)
        async with client:
            return await client.list_tools()

    return _run_async(_action())


def call_tool(name: str, args: Optional[Dict[str, Any]] = None, mcp_url: Optional[str] = None) -> Any:
    """Synchronous helper to call a tool on the MCP server."""
    async def _action():
        client = MCPClient(mcp_url)
        async with client:
            return await client.call_tool(name, args)

    return _run_async(_action())
