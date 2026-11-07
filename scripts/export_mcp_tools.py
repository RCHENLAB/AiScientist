"""Export a server's MCP tool schemas for review and the pinned client registry.

Run this in the server's released image, where MCP and the server are installed:

    python export_mcp_tools.py --server atlas-expression-exploration-mcp > tools.json

The common 0.0.2 CLI's ``tools`` command omits input schemas. This helper uses the
same stdio MCP client but retains them, including all pages. It only initializes
the server and lists tools; it never calls an analysis tool. The gateway does not
import this script or need the MCP package.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shlex
import shutil


async def list_tool_schemas(client) -> list[dict]:
    """Collect tools/list pages without accepting duplicate names or cursor loops."""
    tools, names, cursors = [], set(), set()
    cursor = None
    while True:
        page = await client.list_tools(cursor=cursor)
        for tool in page.tools:
            data = tool.model_dump(by_alias=True, exclude_none=True)
            if data["name"] in names:
                raise ValueError(f"Duplicate MCP tool name: {data['name']}")
            names.add(data["name"])
            tools.append({"name": data["name"], "description": data.get("description", ""),
                          "inputSchema": data["inputSchema"]})
        cursor = page.next_cursor
        if cursor is None:
            return tools
        if cursor in cursors:
            raise ValueError("MCP tools/list repeated a pagination cursor")
        cursors.add(cursor)


async def export_tools(server_command: str, timeout: float) -> list[dict]:
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    argv = shlex.split(server_command)
    if not argv:
        raise ValueError("--server must name a server executable")
    params = StdioServerParameters(command=shutil.which(argv[0]) or argv[0],
                                   args=argv[1:], env=dict(os.environ))
    async with Client(params, read_timeout_seconds=timeout) as client:
        return await list_tool_schemas(client)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", required=True, help="stdio server command (parsed as argv, never a shell)")
    parser.add_argument("--timeout", type=float, default=60, help="total timeout in seconds (default: 60)")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite positive number")

    async def run() -> list[dict]:
        return await asyncio.wait_for(export_tools(args.server, args.timeout), timeout=args.timeout)

    result = asyncio.run(run())
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
