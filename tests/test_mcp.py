"""Talk to the server over real MCP stdio - verifies the transport is clean."""
from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "ansa_mcp_server.py")
PYTHON = sys.executable


async def main() -> int:
    params = StdioServerParameters(command=PYTHON, args=[SERVER],
                                   env={**os.environ})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"server: {init.serverInfo.name} {init.serverInfo.version}")
            print(f"instructions given: {bool(init.instructions)}")

            tools = await session.list_tools()
            print(f"\n{len(tools.tools)} tools:")
            for t in tools.tools:
                first_line = (t.description or "").strip().splitlines()[0]
                print(f"  - {t.name:22s} {first_line[:70]}")

            print("\n--- calling ansa_status (must not launch ANSA) ---")
            res = await session.call_tool("ansa_status", {})
            print(res.content[0].text[:400])

            print("\n--- calling ansa_api_search ---")
            res = await session.call_tool("ansa_api_search",
                                          {"query": "create entity node", "top_n": 2})
            print(res.content[0].text[:500])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
