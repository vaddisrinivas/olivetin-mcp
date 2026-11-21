"""Opt-in tests against the bundled safe OliveTin example, never a personal action catalog.

Set OLIVETIN_TEST_URL to an isolated OliveTin running examples/olivetin/config.yaml.
Set MCP_TEST_URL additionally to exercise a running HTTP or SSE bridge.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport


@pytest.mark.parametrize("transport", ["stdio", "network"])
async def test_demo_server_round_trip(transport):
    upstream = os.environ.get("OLIVETIN_TEST_URL")
    if not upstream:
        pytest.skip("OLIVETIN_TEST_URL must point to the isolated demo server")
    if transport == "network":
        address = os.environ.get("MCP_TEST_URL")
        if not address:
            pytest.skip("MCP_TEST_URL not set")
        connection = address
    else:
        connection = PythonStdioTransport(
            Path(__file__).resolve().parents[1] / "main.py",
            env={"OLIVETIN_URL": upstream, "SECRETS_DIR": "/nonexistent"},
        )

    async def approve_demo(_message, _response_type, _params, _context):
        # This client only connects to an explicitly selected, isolated harmless fixture.
        return True

    async with Client(connection, elicitation_handler=approve_demo, timeout=10) as client:
        tools = {t.name for t in await client.list_tools()}
        assert {"ro_hello", "ro_echo", "get_execution_queue"} <= tools
        result = await client.call_tool("ro_hello", {})
        assert result.content[0].text == "Hello community!"
        result = await client.call_tool("ro_echo", {})
        assert result.content[0].text == "community"
        result = await client.call_tool("ro_echo", {"message": "community.example"})
        assert result.content[0].text == "community.example"
        history = await client.call_tool("get_execution_logs", {})
        assert json.loads(history.content[0].text)["logs"]
        info = await client.call_tool("get_server_info", {})
        assert json.loads(info.content[0].text)["currentVersion"] == "3000.22.0"
        queue = await client.call_tool("get_execution_queue", {})
        assert "groups" in json.loads(queue.content[0].text)
        started = await client.call_tool(
            "start_action_async", {"action_id": "ro_echo", "arguments": {"message": "async_demo"}}
        )
        tracking_id = json.loads(started.content[0].text)["executionTrackingId"]
        for _ in range(100):
            status = await client.call_tool(
                "get_execution_status", {"execution_tracking_id": tracking_id}
            )
            entry = json.loads(status.content[0].text)["logEntry"]
            if entry.get("executionFinished"):
                break
            await asyncio.sleep(0.05)
        assert entry["executionFinished"]
        assert entry["output"].strip() == "async_demo"
        assert entry["exitCode"] == 0
