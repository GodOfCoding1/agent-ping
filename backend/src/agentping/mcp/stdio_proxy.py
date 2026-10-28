"""`agentping-mcp-stdio`: stdio MCP server that forwards to the AgentPing HTTP endpoint.

For MCP clients that can only launch local stdio servers. It holds no database
credentials; it authenticates to the remote server with an agent token.

Environment:
    AGENTPING_MCP_URL     e.g. https://agentping.example.com/mcp
    AGENTPING_TOKEN       agent token (apa_...)
    AGENTPING_TOKEN_FILE  alternative to AGENTPING_TOKEN: path to a file holding the token
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import anyio
import httpx2
from mcp import Client, stdio_server, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel.server import Server

from agentping import __version__

INSTRUCTIONS = (
    "AgentPing delivers notifications, reminders and alarms to the user's Android phone. "
    "Call get_schedule_context first when interpreting relative or local times."
)


def _token() -> str:
    token = os.environ.get("AGENTPING_TOKEN", "").strip()
    token_file = os.environ.get("AGENTPING_TOKEN_FILE", "").strip()
    if not token and token_file:
        token = Path(token_file).expanduser().read_text().strip()
    if not token:
        raise SystemExit("Set AGENTPING_TOKEN or AGENTPING_TOKEN_FILE")
    return token


async def serve(url: str, token: str) -> None:
    http = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(30.0, read=300.0),
    )
    async with http, Client(streamable_http_client(url, http_client=http), cache=None) as upstream:

        async def list_tools(
            _: Any, params: types.PaginatedRequestParams | None
        ) -> types.ListToolsResult:
            return await upstream.list_tools(cursor=params.cursor if params else None)

        async def call_tool(_: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
            return await upstream.call_tool(params.name, params.arguments or {})

        server: Server[Any] = Server(
            "agentping",
            version=__version__,
            instructions=INSTRUCTIONS,
            on_list_tools=list_tools,
            on_call_tool=call_tool,
        )
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    url = os.environ.get("AGENTPING_MCP_URL", "").strip()
    if not url:
        print("Set AGENTPING_MCP_URL (e.g. https://host/mcp)", file=sys.stderr)
        raise SystemExit(2)
    anyio.run(serve, url, _token())


if __name__ == "__main__":
    main()
