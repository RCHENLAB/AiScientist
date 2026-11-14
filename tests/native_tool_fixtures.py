"""Offline execution doubles with the actual packaged native wire contracts.

Only injected orchestration tests use these executors. Production registration still
requires its real runner, skill policy and configured image; no jobs or models execute.
"""
from copy import deepcopy
from aiscientist.agents.mcp_bridge import configured_servers, MCPInputSchema
from aiscientist.agents.research_harness import default_catalog, HarnessTool

def _executor(name):
    def execute(args, ctx):
        return {"status": "ok", "tool": name, "cells": 400, "effect_count": 2}
    return execute

def fixture_catalog():
    return [HarnessTool(server.planner_name(tool["name"]), tool["description"], MCPInputSchema(deepcopy(tool["inputSchema"])),
                        _executor(server.planner_name(tool["name"])))
            for server in configured_servers() for tool in server.tools
            if server.adapter == "analysis"] + default_catalog()
