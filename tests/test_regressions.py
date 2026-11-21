"""Behavioral regressions across the real HTTP and MCP boundaries."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import Client, FastMCP

import main
from main import ActionRegistry, ApprovalManager, OliveTinClient, Settings


@pytest.mark.parametrize(
    "endpoint", ["StartAction", "StartActionAndWait", "RestartAction", "KillAction", "Unknown"]
)
async def test_uncertain_write_is_never_retried(endpoint):
    calls = 0

    async def lost_response(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("The server may already have executed the action", request=request)

    client = OliveTinClient(Settings())
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(lost_response))
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.call(endpoint, {})
        assert calls == 1
    finally:
        await client.close()


def test_arguments_normalized_defaults_and_boolean_wire_values():
    action = {
        "bindingId": "demo",
        "arguments": [
            {"name": "count", "type": "int", "defaultValue": "3"},
            {"name": "enabled", "type": "bool", "defaultValue": "false"},
            {"name": "host", "type": "ascii_identifier"},
        ],
    }
    assert ActionRegistry._validated_arguments(action, {"host": "host.example"}) == [
        {"name": "count", "value": "3"},
        {"name": "enabled", "value": "false"},
        {"name": "host", "value": "host.example"},
    ]


@pytest.mark.parametrize("values", [{"env": "unknown"}, {"env": "prod", "extra": "secret"}])
async def test_async_start_rejects_invalid_arguments_before_http(values):
    action = {
        "bindingId": "deploy",
        "arguments": [
            {"name": "env", "type": "unicode", "choices": [{"value": "prod"}, {"value": "dev"}]},
        ],
    }
    with (
        patch.object(main._registry, "get", return_value=action),
        patch.object(main._client, "call", new_callable=AsyncMock) as call,
    ):
        result = await main.start_action_async("deploy", values)
    assert result.startswith("Validation error:")
    call.assert_not_called()


async def test_prompt_registration_render_and_removal_with_real_fastmcp():
    server = FastMCP("prompt-test")
    registry = ActionRegistry(server, Settings(), OliveTinClient(Settings()))
    action = {
        "bindingId": "greet",
        "title": "Greeting",
        "arguments": [],
        "promptTemplate": "Hello {name}.",
    }
    await registry._rebuild({"greet": action})
    async with Client(server) as client:
        prompts = await client.list_prompts()
        assert [p.name for p in prompts] == ["olivetin_greet"]
        result = await client.get_prompt("olivetin_greet", {"name": "Community"})
        assert result.messages[0].content.text == "Hello Community."
        await registry._rebuild({})
        assert await client.list_prompts() == []


async def test_dynamic_tool_with_real_fastmcp_uses_validated_defaults():
    server = FastMCP("action-test")
    api = OliveTinClient(Settings())
    registry = ActionRegistry(server, Settings(), api)
    action = {
        "bindingId": "ro_demo",
        "arguments": [{"name": "enabled", "type": "bool", "defaultValue": "true"}],
    }
    registry._actions = {"ro_demo": action}
    await registry._rebuild(registry._actions)
    with patch.object(api, "call", return_value={"logEntry": {"output": "ok"}}) as call:
        async with Client(server) as client:
            result = await client.call_tool("ro_demo", {})
    assert result.content[0].text == "ok"
    assert call.call_args.args[1]["arguments"] == [{"name": "enabled", "value": "true"}]


def test_catalog_cannot_shadow_a_static_tool():
    actions = {}
    ActionRegistry._walk_dashboard([{"action": {"bindingId": "list_olivetin_actions"}}], actions)
    assert actions == {}


async def test_expired_approval_cannot_win_event_loop_timeout_race():
    approvals = ApprovalManager(Settings())
    future = asyncio.get_running_loop().create_future()
    approvals._pending["expired"] = {"future": future, "expires_at": time.time() - 1}
    assert approvals.approve("expired") is False
    assert not future.done()


async def test_invalid_catalog_refresh_preserves_previous_tools_and_policy():
    server = FastMCP("catalog-test")
    api = OliveTinClient(Settings())
    registry = ActionRegistry(server, Settings(), api)
    valid = {"bindingId": "ro_good", "arguments": []}
    invalid = {"bindingId": "broken", "arguments": [{"type": "int"}]}
    with patch.object(api, "call", return_value={"dashboard": {"contents": [{"action": valid}]}}):
        await registry.reload()
    with patch.object(api, "call", return_value={"dashboard": {"contents": [{"action": invalid}]}}):
        await registry.reload()
    assert await registry.snapshot() == [valid]
    async with Client(server) as client:
        assert [t.name for t in await client.list_tools()] == ["ro_good"]
    assert "ro_good" in registry.readonly_tool_names


async def test_read_rpc_retry_and_trailing_slash():
    requests = []

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200, json={"logs": []})

    api = OliveTinClient(Settings(OLIVETIN_URL="http://example.test/"))
    api._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert json.dumps(await api.call("GetLogs", {})) == '{"logs": []}'
        assert len(requests) == 2
        assert str(requests[-1].url) == "http://example.test/api/GetLogs"
    finally:
        await api.close()


async def test_null_named_dashboard_falls_back_to_default():
    server = FastMCP("dashboard-test")
    api = OliveTinClient(Settings())
    registry = ActionRegistry(server, Settings(), api)
    with patch.object(
        api,
        "call",
        side_effect=[
            {"dashboard": None},
            {"dashboard": {"contents": [{"action": {"bindingId": "ro_demo"}}]}},
        ],
    ):
        await registry.reload()
    assert await registry.known("ro_demo")


@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_real_mcp_middleware_obeys_approval_decision(approved, mode):
    server = FastMCP("approval-test")
    server.add_middleware(main._ApprovalMiddleware())
    executed = []

    @server.tool
    async def destructive_demo() -> str:
        executed.append(True)
        return "executed"

    async def handler(_message, _response_type, _params, _context):
        return approved

    async with Client(server, mode=mode, elicitation_handler=handler, timeout=3) as client:
        result = await client.call_tool("destructive_demo", {}, raise_on_error=False)
    assert bool(executed) is approved
    assert ("executed" in result.content[0].text) is approved


@pytest.mark.parametrize(
    "case",
    ["valid", "replay", "expired", "changed_args", "changed_tool", "missing_state", "declined"],
)
def test_modern_approval_continuation_is_bound_and_single_use(case):
    approvals = ApprovalManager(Settings())
    ctx = SimpleNamespace(request_state=None, input_responses=None)
    ask = approvals.modern_approval("deploy", {"target": "demo"}, ctx)
    ctx.request_state = ask.input_required.request_state
    ctx.input_responses = {
        "olivetin_approval": main.mt.ElicitResult(
            action="decline" if case == "declined" else "accept",
            content={"value": True},
        )
    }
    args = {"target": "changed" if case == "changed_args" else "demo"}
    tool = "different" if case == "changed_tool" else "deploy"
    if case == "replay":
        assert approvals.modern_approval(tool, args, ctx) is True
    if case == "expired":
        approvals._inline_pending = {
            k: (v[0], v[1], 0) for k, v in approvals._inline_pending.items()
        }
    if case == "missing_state":
        ctx.request_state = None
    assert approvals.modern_approval(tool, args, ctx) is (case == "valid")


async def test_execution_queue_tool_calls_documented_rpc():
    with patch.object(main._client, "call", return_value={"groups": [], "totalActive": 0}) as call:
        assert json.loads(await main.get_execution_queue())["totalActive"] == 0
    call.assert_awaited_once_with("GetExecutionQueue", {})
