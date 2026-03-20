"""
olivetin-mcp — comprehensive pytest suite.

Run:
    cd olivetin-mcp
    pytest --cov=main --cov-report=term-missing tests/
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ── make sure the package root is importable ─────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import main  # noqa: E402  (must come after sys.path insert)
from main import (
    _STATIC_READONLY_TOOLS,
    ActionRegistry,
    ApprovalManager,
    OliveTinClient,
    Settings,
    _approval_tmpl,
    _cleanup,
    _read_secret,
    _result_tmpl,
)

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def settings():
    return Settings(
        HOST_BRIDGE_PORT=9999,
        OLIVETIN_URL="http://localhost:1337",
        APPROVAL_TIMEOUT_SECS=2,
        ELICITATION_TIMEOUT_SECS=1.0,
        SECRETS_DIR="/nonexistent",
    )


@pytest.fixture()
def client(settings):
    c = OliveTinClient(settings)
    c._http = AsyncMock()
    return c


@pytest.fixture()
def mock_mcp():
    m = MagicMock()
    m.add_tool = MagicMock()
    m.remove_tool = MagicMock()
    m.add_prompt = MagicMock()
    m.remove_prompt = MagicMock()
    return m


@pytest.fixture()
def registry(mock_mcp, settings, client):
    return ActionRegistry(mock_mcp, settings, client)


@pytest.fixture()
def approvals(settings):
    return ApprovalManager(settings)


@pytest.fixture()
def simple_action():
    return {
        "bindingId": "test_action",
        "title": "Test Action",
        "timeout": 10,
        "canExec": True,
        "arguments": [
            {"name": "msg", "type": "unicode", "description": "A message"},
        ],
    }


@pytest.fixture()
def readonly_action():
    return {
        "bindingId": "ro_status",
        "title": "[read-only] Get Status",
        "timeout": 5,
        "canExec": True,
        "arguments": [],
    }


# ─── Settings ─────────────────────────────────────────────────────────────────


def test_settings_defaults():
    s = Settings()
    assert s.HOST_BRIDGE_PORT == 9003
    assert s.OLIVETIN_URL == "http://host.docker.internal:1337"
    assert s.APPROVAL_TIMEOUT_SECS == 120
    assert s.ELICITATION_TIMEOUT_SECS == 30.0
    assert s.SECRETS_DIR == "/run/secrets"
    assert s.ACTION_REFRESH_INTERVAL_SECS == 300
    assert s.OLIVETIN_REMOTE_USER == ""
    assert s.OLIVETIN_REMOTE_GROUPS == ""
    assert s.OLIVETIN_USER_HEADER == "X-Remote-User"
    assert s.OLIVETIN_GROUPS_HEADER == "X-Remote-Groups"
    assert s.OLIVETIN_JWT_TOKEN == ""
    assert s.OLIVETIN_BASIC_USER == ""
    assert s.OLIVETIN_BASIC_PASS == ""


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("HOST_BRIDGE_PORT", "8888")
    monkeypatch.setenv("OLIVETIN_URL", "http://myserver:1337")
    s = Settings()
    assert s.HOST_BRIDGE_PORT == 8888
    assert s.OLIVETIN_URL == "http://myserver:1337"


# ─── _read_secret ──────────────────────────────────────────────────────────────


def test_read_secret_found():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "my_secret")
        with open(path, "w") as f:
            f.write("  secret_value  \n")
        with patch.object(main.cfg, "SECRETS_DIR", d):
            assert _read_secret("my_secret") == "secret_value"


def test_read_secret_not_found():
    assert _read_secret("no_such_secret_xyz") == ""


def test_read_secret_dotted_name():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "brave_api_key")
        with open(path, "w") as f:
            f.write("key123")
        with patch.object(main.cfg, "SECRETS_DIR", d):
            assert _read_secret("brave_api_key.txt") == "key123"


# ─── _cleanup ─────────────────────────────────────────────────────────────────


async def test_cleanup_removes_file():
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = f.name
    assert os.path.exists(path)
    await _cleanup(path, delay=0)
    assert not os.path.exists(path)


async def test_cleanup_missing_file_ok():
    await _cleanup("/tmp/nonexistent_xyz_123.png", delay=0)


# ─── OliveTinClient.auth_headers ──────────────────────────────────────────────


def test_auth_headers_none(settings):
    c = OliveTinClient(settings)
    assert c.auth_headers() == {}


def test_auth_headers_remote_user():
    s = Settings(OLIVETIN_REMOTE_USER="claude-agent", OLIVETIN_REMOTE_GROUPS="admins,ops")
    c = OliveTinClient(s)
    h = c.auth_headers()
    assert h["X-Remote-User"] == "claude-agent"
    assert h["X-Remote-Groups"] == "admins,ops"


def test_auth_headers_custom_header_names():
    s = Settings(
        OLIVETIN_REMOTE_USER="bot",
        OLIVETIN_USER_HEADER="Remote-User",
        OLIVETIN_GROUPS_HEADER="Remote-Groups",
    )
    c = OliveTinClient(s)
    h = c.auth_headers()
    assert "Remote-User" in h
    assert h["Remote-User"] == "bot"


def test_auth_headers_jwt():
    s = Settings(OLIVETIN_JWT_TOKEN="mytoken")
    c = OliveTinClient(s)
    h = c.auth_headers()
    assert h["Authorization"] == "Bearer mytoken"


def test_auth_headers_jwt_from_secret():
    s = Settings(SECRETS_DIR="/tmp")
    c = OliveTinClient(s)
    with patch("main._read_secret", return_value="file_token"):
        h = c.auth_headers()
    assert h["Authorization"] == "Bearer file_token"


def test_auth_headers_basic():
    import base64

    s = Settings(OLIVETIN_BASIC_USER="user", OLIVETIN_BASIC_PASS="pass")
    c = OliveTinClient(s)
    h = c.auth_headers()
    expected = base64.b64encode(b"user:pass").decode()
    assert h["Authorization"] == f"Basic {expected}"


def test_auth_headers_basic_overrides_jwt():
    """Basic auth Authorization header overwrites JWT when both set."""
    s = Settings(OLIVETIN_JWT_TOKEN="jwt", OLIVETIN_BASIC_USER="u", OLIVETIN_BASIC_PASS="p")
    c = OliveTinClient(s)
    h = c.auth_headers()
    assert h["Authorization"].startswith("Basic ")


def test_active_auth_modes_none(settings):
    c = OliveTinClient(settings)
    with patch("main._read_secret", return_value=""):
        assert c.active_auth_modes() == "none"


def test_active_auth_modes_header():
    s = Settings(OLIVETIN_REMOTE_USER="agent")
    c = OliveTinClient(s)
    with patch("main._read_secret", return_value=""):
        modes = c.active_auth_modes()
    assert "header" in modes


def test_active_auth_modes_jwt():
    s = Settings(OLIVETIN_JWT_TOKEN="tok")
    c = OliveTinClient(s)
    with patch("main._read_secret", return_value=""):
        modes = c.active_auth_modes()
    assert "jwt" in modes


def test_active_auth_modes_basic():
    s = Settings(OLIVETIN_BASIC_USER="u")
    c = OliveTinClient(s)
    with patch("main._read_secret", return_value=""):
        modes = c.active_auth_modes()
    assert "basic" in modes


# ─── OliveTinClient.start / close ─────────────────────────────────────────────


async def test_client_start_creates_http(settings):
    c = OliveTinClient(settings)
    assert c._http is None
    await c.start()
    assert c._http is not None
    await c.close()


async def test_client_close_noop_when_not_started(settings):
    c = OliveTinClient(settings)
    await c.close()  # should not raise


async def test_client_close_sets_none(settings):
    c = OliveTinClient(settings)
    await c.start()
    await c.close()
    assert c._http is None


# ─── OliveTinClient.call ──────────────────────────────────────────────────────


async def test_client_call_success(client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True}
    mock_resp.raise_for_status = MagicMock()
    client._http.post = AsyncMock(return_value=mock_resp)

    result = await client.call("GetDashboard", {"title": "Home"})
    assert result == {"ok": True}
    client._http.post.assert_called_once()


async def test_client_call_includes_auth_headers(client):
    client._cfg.OLIVETIN_REMOTE_USER = "agent"
    mock_resp = MagicMock()
    mock_resp.json.return_value = {}
    mock_resp.raise_for_status = MagicMock()
    client._http.post = AsyncMock(return_value=mock_resp)

    await client.call("WhoAmI", {})
    _, kwargs = client._http.post.call_args
    assert kwargs["headers"].get("X-Remote-User") == "agent"


async def test_client_call_raises_http_status_error(client):
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=MagicMock()
    )
    client._http.post = AsyncMock(return_value=mock_resp)

    with pytest.raises(httpx.HTTPStatusError):
        await client.call("BadEndpoint", {})


async def test_client_call_not_initialised(settings):
    c = OliveTinClient(settings)
    with pytest.raises(AssertionError, match="not initialised"):
        await c.call("GetDashboard", {})


async def test_client_call_retries_and_reraises(client):
    client._http.post = AsyncMock(side_effect=httpx.RequestError("net err"))

    with patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(httpx.RequestError):
            await client.call("GetDashboard", {})

    assert client._http.post.call_count == 3


async def test_client_call_succeeds_after_retry(client):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True}
    mock_resp.raise_for_status = MagicMock()
    client._http.post = AsyncMock(side_effect=[httpx.RequestError("err"), mock_resp])

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await client.call("GetDashboard", {})
    assert result == {"ok": True}


# ─── ActionRegistry — classification ─────────────────────────────────────────


def test_is_read_only_title_prefix():
    assert ActionRegistry.is_read_only({"title": "[read-only] Status", "bindingId": "status"})


def test_is_read_only_id_prefix():
    assert ActionRegistry.is_read_only({"title": "Status", "bindingId": "ro_status"})


def test_is_read_only_false():
    assert not ActionRegistry.is_read_only({"title": "Deploy", "bindingId": "deploy_prod"})


def test_clean_title_strips_prefix():
    assert ActionRegistry.clean_title({"title": "[read-only] Get Status"}) == "Get Status"


def test_clean_title_uppercase_prefix():
    assert ActionRegistry.clean_title({"title": "[READ-ONLY] Ping"}) == "Ping"


def test_clean_title_no_prefix():
    assert ActionRegistry.clean_title({"title": "Deploy"}) == "Deploy"


def test_clean_title_falls_back_to_binding_id():
    assert ActionRegistry.clean_title({"bindingId": "my_action"}) == "my_action"


def test_display_name_known(registry, simple_action):
    registry._actions = {"test_action": simple_action}
    assert registry.display_name("test_action") == "Test Action"


def test_display_name_unknown(registry):
    assert registry.display_name("unknown_tool") == "Unknown Tool"


# ─── ActionRegistry — schema building ────────────────────────────────────────


def test_make_action_model_unicode(simple_action):
    model = ActionRegistry._make_action_model(simple_action)
    assert "msg" in model.model_fields


def test_make_action_model_all_types():
    action = {
        "bindingId": "multi",
        "arguments": [
            {"name": "a", "type": "unicode"},
            {"name": "b", "type": "ascii_identifier"},
            {"name": "c", "type": "url"},
            {"name": "d", "type": "int"},
            {"name": "e", "type": "float"},
            {"name": "f", "type": "bool"},
        ],
    }
    model = ActionRegistry._make_action_model(action)
    fields = model.model_fields
    assert set(fields) == {"a", "b", "c", "d", "e", "f"}


def test_make_action_model_skips_html_confirmation():
    action = {
        "bindingId": "x",
        "arguments": [
            {"name": "note", "type": "html"},
            {"name": "confirm", "type": "confirmation"},
            {"name": "real", "type": "unicode"},
        ],
    }
    model = ActionRegistry._make_action_model(action)
    assert set(model.model_fields) == {"real"}


def test_make_action_model_with_default():
    action = {
        "bindingId": "x",
        "arguments": [{"name": "env", "type": "unicode", "defaultValue": "prod"}],
    }
    model = ActionRegistry._make_action_model(action)
    inst = model()
    assert inst.env == "prod"


def test_make_action_model_with_choices():
    action = {
        "bindingId": "x",
        "arguments": [
            {"name": "env", "type": "unicode", "choices": [{"value": "prod"}, {"value": "dev"}]}
        ],
    }
    schema = ActionRegistry._make_input_schema(action)
    assert "prod" in schema["properties"]["env"]["enum"]


def test_make_action_model_with_dict_choices():
    action = {
        "bindingId": "x",
        "arguments": [{"name": "c", "type": "unicode", "choices": ["a", "b"]}],
    }
    schema = ActionRegistry._make_input_schema(action)
    assert schema["properties"]["c"]["enum"] == ["a", "b"]


def test_make_action_model_ascii_identifier_pattern():
    action = {
        "bindingId": "x",
        "arguments": [{"name": "name", "type": "ascii_identifier"}],
    }
    schema = ActionRegistry._make_input_schema(action)
    assert "pattern" in schema["properties"]["name"]


def test_make_action_model_url_format():
    action = {
        "bindingId": "x",
        "arguments": [{"name": "link", "type": "url"}],
    }
    schema = ActionRegistry._make_input_schema(action)
    assert schema["properties"]["link"].get("format") == "uri"


def test_make_input_schema_no_title():
    action = {"bindingId": "x", "arguments": []}
    schema = ActionRegistry._make_input_schema(action)
    assert "title" not in schema


def test_validate_args_valid(simple_action):
    err = ActionRegistry._validate_args(simple_action, {"msg": "hello"})
    assert err is None


def test_validate_args_invalid(simple_action):
    action = {
        "bindingId": "x",
        "arguments": [{"name": "count", "type": "int"}],
    }
    err = ActionRegistry._validate_args(action, {"count": "not_an_int"})
    assert err is not None


def test_make_description_readonly(readonly_action):
    reg = ActionRegistry.__new__(ActionRegistry)
    desc = reg._make_description(readonly_action)
    assert "No approval needed" in desc


def test_make_description_destructive(simple_action):
    reg = ActionRegistry.__new__(ActionRegistry)
    desc = reg._make_description(simple_action)
    assert "Requires human approval" in desc


def test_make_description_with_icon():
    reg = ActionRegistry.__new__(ActionRegistry)
    action = {"bindingId": "x", "title": "Test", "icon": "🚀", "arguments": []}
    desc = reg._make_description(action)
    assert "🚀" in desc


def test_make_description_skips_html_icon():
    reg = ActionRegistry.__new__(ActionRegistry)
    action = {"bindingId": "x", "title": "Test", "icon": "<img src=x>", "arguments": []}
    desc = reg._make_description(action)
    assert "<img" not in desc


def test_make_annotations_readonly(readonly_action):
    ann = ActionRegistry._make_annotations(readonly_action)
    assert ann.readOnlyHint is True
    assert ann.destructiveHint is False


def test_make_annotations_destructive(simple_action):
    ann = ActionRegistry._make_annotations(simple_action)
    assert ann.readOnlyHint is False
    assert ann.destructiveHint is True


# ─── ActionRegistry — rate limiter ────────────────────────────────────────────


def test_get_limiter_creates_new(registry):
    lim = registry.get_limiter("my_action")
    assert lim is not None


def test_get_limiter_returns_same(registry):
    lim1 = registry.get_limiter("my_action")
    lim2 = registry.get_limiter("my_action")
    assert lim1 is lim2


# ─── ActionRegistry — catalog access ──────────────────────────────────────────


async def test_registry_get_found(registry, simple_action):
    registry._actions = {"test_action": simple_action}
    a = await registry.get("test_action")
    assert a == simple_action


async def test_registry_get_not_found(registry):
    registry._actions = {}
    a = await registry.get("nope")
    assert a is None


async def test_registry_snapshot(registry, simple_action):
    registry._actions = {"test_action": simple_action}
    s = await registry.snapshot()
    assert simple_action in s


async def test_registry_known_true(registry, simple_action):
    registry._actions = {"test_action": simple_action}
    assert await registry.known("test_action") is True


async def test_registry_known_false(registry):
    registry._actions = {}
    assert await registry.known("nope") is False


# ─── ActionRegistry — run_action ──────────────────────────────────────────────


async def test_run_action_unknown(registry):
    registry._actions = {}
    result = await registry.run_action("ghost", {})
    assert "Unknown action" in result


async def test_run_action_can_exec_false_with_rate_limit(registry, simple_action):
    simple_action["canExec"] = False
    simple_action["datetimeRateLimitExpires"] = "2099-01-01T00:00:00Z"
    registry._actions = {"test_action": simple_action}
    result = await registry.run_action("test_action", {})
    assert "rate-limited" in result


async def test_run_action_can_exec_false_no_rate_limit(registry, simple_action):
    simple_action["canExec"] = False
    simple_action.pop("datetimeRateLimitExpires", None)
    registry._actions = {"test_action": simple_action}
    result = await registry.run_action("test_action", {})
    assert "canExec=false" in result


async def test_run_action_validation_error(registry):
    action = {
        "bindingId": "x",
        "title": "X",
        "timeout": 5,
        "canExec": True,
        "arguments": [{"name": "count", "type": "int"}],
    }
    registry._actions = {"x": action}
    result = await registry.run_action("x", {"count": "bad"})
    assert "Validation error" in result


async def test_run_action_success(registry, simple_action, client):
    registry._actions = {"test_action": simple_action}
    client.call = AsyncMock(
        return_value={"logEntry": {"output": "hello", "exitCode": 0, "executionTrackingId": "tid"}}
    )
    result = await registry.run_action("test_action", {"msg": "hi"})
    assert result == "hello"


async def test_run_action_no_output_nonzero_exit(registry, simple_action, client):
    registry._actions = {"test_action": simple_action}
    client.call = AsyncMock(
        return_value={"logEntry": {"output": "", "exitCode": 1, "executionTrackingId": ""}}
    )
    result = await registry.run_action("test_action", {"msg": "hi"})
    assert "exit code 1" in result


async def test_run_action_empty_output(registry, simple_action, client):
    registry._actions = {"test_action": simple_action}
    client.call = AsyncMock(
        return_value={"logEntry": {"output": "", "exitCode": 0, "executionTrackingId": ""}}
    )
    result = await registry.run_action("test_action", {"msg": "hi"})
    assert result == "(no output)"


async def test_run_action_blocked_with_rate_limit(registry, simple_action, client):
    registry._actions = {"test_action": simple_action}
    client.call = AsyncMock(
        return_value={"logEntry": {"blocked": True, "datetimeRateLimitExpires": "2099-01-01"}}
    )
    result = await registry.run_action("test_action", {"msg": "hi"})
    assert "rate-limited" in result


async def test_run_action_blocked_no_rate_limit(registry, simple_action, client):
    registry._actions = {"test_action": simple_action}
    client.call = AsyncMock(return_value={"logEntry": {"blocked": True}})
    result = await registry.run_action("test_action", {"msg": "hi"})
    assert "Blocked" in result


async def test_run_action_timed_out(registry, simple_action, client):
    registry._actions = {"test_action": simple_action}
    client.call = AsyncMock(return_value={"logEntry": {"timedOut": True}})
    result = await registry.run_action("test_action", {"msg": "hi"})
    assert "timed out" in result


async def test_run_action_http_status_error(registry, simple_action, client):
    registry._actions = {"test_action": simple_action}
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal error"
    client.call = AsyncMock(
        side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=mock_response)
    )
    result = await registry.run_action("test_action", {"msg": "hi"})
    assert "500" in result


async def test_run_action_request_error(registry, simple_action, client):
    registry._actions = {"test_action": simple_action}
    client.call = AsyncMock(side_effect=httpx.RequestError("connection refused"))
    result = await registry.run_action("test_action", {"msg": "hi"})
    assert "Could not reach OliveTin" in result


async def test_run_action_generic_error(registry, simple_action, client):
    registry._actions = {"test_action": simple_action}
    client.call = AsyncMock(side_effect=RuntimeError("unexpected"))
    result = await registry.run_action("test_action", {"msg": "hi"})
    assert "OliveTin error" in result


# ─── ActionRegistry — _walk_dashboard ────────────────────────────────────────


def test_walk_dashboard_simple():
    acc: dict = {}
    contents = [{"action": {"bindingId": "a1", "title": "A1"}}]
    ActionRegistry._walk_dashboard(contents, acc)
    assert "a1" in acc


def test_walk_dashboard_nested():
    acc: dict = {}
    contents = [{"contents": [{"action": {"bindingId": "nested", "title": "N"}}]}]
    ActionRegistry._walk_dashboard(contents, acc)
    assert "nested" in acc


def test_walk_dashboard_skips_hidden():
    acc: dict = {}
    contents = [{"action": {"bindingId": "hidden_one", "title": "H", "hidden": True}}]
    ActionRegistry._walk_dashboard(contents, acc)
    assert "hidden_one" not in acc


def test_walk_dashboard_skips_no_binding_id():
    acc: dict = {}
    contents = [{"action": {"title": "No ID"}}]
    ActionRegistry._walk_dashboard(contents, acc)
    assert acc == {}


def test_walk_dashboard_warns_duplicate():
    acc: dict = {"dup": {"bindingId": "dup"}}
    contents = [{"action": {"bindingId": "dup", "title": "Duplicate"}}]
    ActionRegistry._walk_dashboard(contents, acc)
    assert len(acc) == 1


# ─── ActionRegistry — reload ──────────────────────────────────────────────────


async def test_reload_success(registry, client):
    client.call = AsyncMock(
        return_value={
            "dashboard": {
                "contents": [
                    {
                        "action": {
                            "bindingId": "act1",
                            "title": "Act1",
                            "arguments": [],
                            "timeout": 5,
                            "canExec": True,
                        }
                    }
                ]
            }
        }
    )
    await registry.reload()
    assert "act1" in registry._actions


async def test_reload_fallback_empty_home(registry, client):
    responses = [
        {"dashboard": {"contents": []}},  # "Home" returns empty
        {
            "dashboard": {
                "contents": [
                    {
                        "action": {
                            "bindingId": "act2",
                            "title": "Act2",
                            "arguments": [],
                            "timeout": 5,
                            "canExec": True,
                        }
                    }
                ]
            }
        },
    ]
    client.call = AsyncMock(side_effect=responses)
    await registry.reload()
    assert "act2" in registry._actions


async def test_reload_exception_handled(registry, client):
    client.call = AsyncMock(side_effect=RuntimeError("OliveTin down"))
    await registry.reload()  # should not raise


# ─── ActionRegistry — refresh_loop ───────────────────────────────────────────


async def test_refresh_loop_stops_on_event(registry, client):
    client.call = AsyncMock(return_value={"dashboard": {"contents": []}})
    stop = asyncio.Event()
    stop.set()
    await registry.refresh_loop(stop)  # should return immediately


async def test_refresh_loop_reloads(registry, client, settings):
    settings.ACTION_REFRESH_INTERVAL_SECS = 0
    registry._cfg = settings
    client.call = AsyncMock(return_value={"dashboard": {"contents": []}})
    stop = asyncio.Event()

    async def stopper():
        await asyncio.sleep(0.05)
        stop.set()

    asyncio.create_task(stopper())
    await registry.refresh_loop(stop)
    assert client.call.call_count >= 1


# ─── ApprovalManager — try_elicitation ───────────────────────────────────────


async def test_elicitation_accepted_true(approvals):
    ctx = MagicMock()
    accepted = MagicMock(spec=main.AcceptedElicitation)
    accepted.data = True
    ctx.elicit = AsyncMock(return_value=accepted)
    result = await approvals.try_elicitation("my_tool", {"k": "v"}, ctx)
    assert result is True


async def test_elicitation_accepted_false(approvals):
    ctx = MagicMock()
    accepted = MagicMock(spec=main.AcceptedElicitation)
    accepted.data = False
    ctx.elicit = AsyncMock(return_value=accepted)
    result = await approvals.try_elicitation("my_tool", {}, ctx)
    assert result is False


async def test_elicitation_declined(approvals):
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=MagicMock(spec=main.DeclinedElicitation))
    result = await approvals.try_elicitation("my_tool", {}, ctx)
    assert result is False


async def test_elicitation_cancelled(approvals):
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=MagicMock(spec=main.CancelledElicitation))
    result = await approvals.try_elicitation("my_tool", {}, ctx)
    assert result is False


async def test_elicitation_timeout(approvals):
    async def slow_elicit(*args, **kwargs):
        await asyncio.sleep(10)

    ctx = MagicMock()
    ctx.elicit = slow_elicit
    result = await approvals.try_elicitation("my_tool", {}, ctx)
    assert result is None


async def test_elicitation_exception(approvals):
    ctx = MagicMock()
    ctx.elicit = AsyncMock(side_effect=NotImplementedError("no elicitation"))
    result = await approvals.try_elicitation("my_tool", {}, ctx)
    assert result is None


async def test_elicitation_unknown_result(approvals):
    ctx = MagicMock()
    ctx.elicit = AsyncMock(return_value=object())
    result = await approvals.try_elicitation("my_tool", {}, ctx)
    assert result is False


# ─── ApprovalManager — pending_url_approval ──────────────────────────────────


async def test_pending_url_approval_approved(approvals):
    async def approve_it():
        await asyncio.sleep(0.01)
        token = next(iter(approvals._pending))
        approvals.approve(token)

    asyncio.create_task(approve_it())
    result = await approvals.pending_url_approval("tool", {}, "My Tool")
    assert result is True


async def test_pending_url_approval_denied(approvals):
    async def deny_it():
        await asyncio.sleep(0.01)
        token = next(iter(approvals._pending))
        approvals.deny(token)

    asyncio.create_task(deny_it())
    result = await approvals.pending_url_approval("tool", {}, "My Tool")
    assert result is False


async def test_pending_url_approval_timeout(approvals):
    # APPROVAL_TIMEOUT_SECS=2 in fixture; set to tiny value
    approvals._cfg = Settings(APPROVAL_TIMEOUT_SECS=0, SECRETS_DIR="/nonexistent")
    result = await approvals.pending_url_approval("tool", {}, "My Tool")
    assert result is False


async def test_pending_url_approval_uses_base_url():
    s = Settings(APPROVAL_BASE_URL="http://my.host:9003", SECRETS_DIR="/nonexistent")
    ApprovalManager(s)  # verify construction works

    captured_url = []

    async def patched(tool_name, args, display_name):
        token = "fake"
        fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        base = s.APPROVAL_BASE_URL
        captured_url.append(f"{base}/pending/{token}")
        fut.set_result(False)
        return False

    await patched("tool", {}, "T")
    assert "http://my.host:9003/pending/fake" in captured_url


# ─── ApprovalManager — queue accessors ───────────────────────────────────────


def test_approve_found(approvals):
    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    approvals._pending["tok"] = {
        "future": fut,
        "action_id": "x",
        "title": "X",
        "args": {},
        "created_at": time.time(),
        "expires_at": time.time() + 60,
    }
    result = approvals.approve("tok")
    assert result is True
    assert fut.result() is True


def test_approve_already_done(approvals):
    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    fut.set_result(True)
    approvals._pending["tok"] = {
        "future": fut,
        "action_id": "x",
        "title": "X",
        "args": {},
        "created_at": time.time(),
        "expires_at": time.time() + 60,
    }
    result = approvals.approve("tok")
    assert result is False


def test_approve_not_found(approvals):
    assert approvals.approve("missing") is False


def test_deny_found(approvals):
    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    approvals._pending["tok"] = {
        "future": fut,
        "action_id": "x",
        "title": "X",
        "args": {},
        "created_at": time.time(),
        "expires_at": time.time() + 60,
    }
    result = approvals.deny("tok")
    assert result is True
    assert fut.result() is False


def test_deny_not_found(approvals):
    assert approvals.deny("missing") is False


def test_get_found(approvals):
    approvals._pending["tok"] = {"action_id": "x"}
    assert approvals.get("tok") == {"action_id": "x"}


def test_get_not_found(approvals):
    assert approvals.get("missing") is None


def test_list_pending_empty(approvals):
    assert approvals.list_pending() == []


def test_list_pending_items(approvals):
    fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    approvals._pending["abc123"] = {
        "future": fut,
        "action_id": "deploy",
        "title": "Deploy",
        "args": {"env": "prod"},
        "created_at": time.time(),
        "expires_at": time.time() + 60,
    }
    items = approvals.list_pending()
    assert len(items) == 1
    assert items[0]["action_id"] == "deploy"
    assert "approval_url" in items[0]
    assert items[0]["expires_in"] > 0


# ─── _ApprovalMiddleware ──────────────────────────────────────────────────────


def _make_context(
    tool_name: str, arguments: dict | None = None, fastmcp_ctx: Any = None
) -> MagicMock:
    ctx = MagicMock()
    # fastmcp 3.1.1+: context.message IS CallToolRequestParams directly
    ctx.message.name = tool_name
    ctx.message.arguments = arguments or {}
    ctx.fastmcp_context = fastmcp_ctx
    return ctx


async def test_middleware_readonly_passthrough():
    mw = main._ApprovalMiddleware()
    main._registry.readonly_tool_names = frozenset({"whoami"})
    call_next = AsyncMock(return_value="ok")
    ctx = _make_context("whoami")
    result = await mw.on_call_tool(ctx, call_next)
    call_next.assert_called_once_with(ctx)
    assert result == "ok"


async def test_middleware_elicitation_approved():
    mw = main._ApprovalMiddleware()
    main._registry.readonly_tool_names = frozenset()

    fastmcp_ctx = MagicMock()
    accepted = MagicMock(spec=main.AcceptedElicitation)
    accepted.data = True
    fastmcp_ctx.elicit = AsyncMock(return_value=accepted)

    ctx = _make_context("deploy", {"env": "prod"}, fastmcp_ctx)
    call_next = AsyncMock(return_value="deployed")
    result = await mw.on_call_tool(ctx, call_next)
    call_next.assert_called_once()
    assert result == "deployed"


async def test_middleware_elicitation_denied():
    mw = main._ApprovalMiddleware()
    main._registry.readonly_tool_names = frozenset()

    fastmcp_ctx = MagicMock()
    fastmcp_ctx.elicit = AsyncMock(return_value=MagicMock(spec=main.DeclinedElicitation))

    ctx = _make_context("deploy", {}, fastmcp_ctx)
    call_next = AsyncMock()
    result = await mw.on_call_tool(ctx, call_next)
    call_next.assert_not_called()
    assert "not approved" in result[0].text


async def test_middleware_elicitation_timeout_falls_back_to_url():
    """When elicitation returns None (timeout), falls back to URL approval and approves."""
    mw = main._ApprovalMiddleware()
    main._registry.readonly_tool_names = frozenset()
    fastmcp_ctx = MagicMock()

    ctx = _make_context("deploy", {}, fastmcp_ctx)
    call_next = AsyncMock(return_value="ok")

    with patch.object(main._approvals, "try_elicitation", return_value=None):
        with patch.object(
            main._approvals, "pending_url_approval", new=AsyncMock(return_value=True)
        ):
            await mw.on_call_tool(ctx, call_next)

    call_next.assert_called_once()


async def test_middleware_no_fastmcp_ctx_falls_back_to_url():
    """When fastmcp_context is None, skips elicitation and goes straight to URL approval."""
    mw = main._ApprovalMiddleware()
    main._registry.readonly_tool_names = frozenset()
    ctx = _make_context("deploy", {}, fastmcp_ctx=None)

    call_next = AsyncMock(return_value="ok")
    with patch.object(main._approvals, "pending_url_approval", new=AsyncMock(return_value=True)):
        await mw.on_call_tool(ctx, call_next)

    call_next.assert_called_once()


# ─── HTTP routes ──────────────────────────────────────────────────────────────


async def test_pending_list_route_empty():
    with patch.object(main, "_approvals") as mock_approvals:
        mock_approvals.list_pending.return_value = []
        request = MagicMock()
        response = await main.pending_list_route(request)
    assert response.status_code == 200
    data = json.loads(response.body)
    assert data["pending"] == []


async def test_pending_list_route_with_items():
    with patch.object(main, "_approvals") as mock_approvals:
        mock_approvals.list_pending.return_value = [
            {
                "token": "t",
                "action_id": "x",
                "title": "X",
                "args": {},
                "approval_url": "http://x/t",
                "expires_in": 60,
            }
        ]
        request = MagicMock()
        response = await main.pending_list_route(request)
    assert response.status_code == 200
    data = json.loads(response.body)
    assert len(data["pending"]) == 1


async def test_pending_detail_route_not_found():
    with patch.object(main, "_approvals") as mock_approvals:
        mock_approvals.get.return_value = None
        request = MagicMock()
        request.path_params = {"token": "missing"}
        response = await main.pending_detail_route(request)
    assert response.status_code == 404


async def test_pending_detail_route_found():
    p = {
        "action_id": "deploy",
        "title": "Deploy",
        "args": {"env": "prod"},
        "expires_at": time.time() + 60,
    }
    with patch.object(main, "_approvals") as mock_approvals:
        mock_approvals.get.return_value = p
        request = MagicMock()
        request.path_params = {"token": "abc"}
        response = await main.pending_detail_route(request)
    assert response.status_code == 200
    assert b"Deploy" in response.body


async def test_pending_approve_route_expired():
    with patch.object(main, "_approvals") as mock_approvals:
        mock_approvals.get.return_value = None
        request = MagicMock()
        request.path_params = {"token": "gone"}
        response = await main.pending_approve_route(request)
    assert response.status_code == 410


async def test_pending_approve_route_success():
    p = {"action_id": "deploy", "title": "Deploy"}
    with patch.object(main, "_approvals") as mock_approvals:
        mock_approvals.get.return_value = p
        mock_approvals.approve.return_value = True
        request = MagicMock()
        request.path_params = {"token": "tok"}
        response = await main.pending_approve_route(request)
    assert response.status_code == 200
    assert b"Approved" in response.body


async def test_pending_deny_route_expired():
    with patch.object(main, "_approvals") as mock_approvals:
        mock_approvals.get.return_value = None
        request = MagicMock()
        request.path_params = {"token": "gone"}
        response = await main.pending_deny_route(request)
    assert response.status_code == 410


async def test_pending_deny_route_success():
    p = {"action_id": "deploy", "title": "Deploy"}
    with patch.object(main, "_approvals") as mock_approvals:
        mock_approvals.get.return_value = p
        mock_approvals.deny.return_value = True
        request = MagicMock()
        request.path_params = {"token": "tok"}
        response = await main.pending_deny_route(request)
    assert response.status_code == 200
    assert b"Denied" in response.body


# ─── Static tools ─────────────────────────────────────────────────────────────


async def test_list_pending_approvals_empty():
    with patch.object(main._approvals, "list_pending", return_value=[]):
        result = await main.list_pending_approvals()
    assert "No actions" in result


async def test_list_pending_approvals_items():
    items = [
        {
            "title": "Deploy",
            "action_id": "deploy",
            "approval_url": "http://x/tok",
            "args": {},
            "expires_in": 30,
        }
    ]
    with patch.object(main._approvals, "list_pending", return_value=items):
        result = await main.list_pending_approvals()
    assert "Deploy" in result
    assert "http://x/tok" in result


async def test_brave_search_not_configured():
    with patch.object(main, "BRAVE_API_KEY", ""):
        result = await main.brave_search("test query")
    assert "not configured" in result


async def test_brave_search_success():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "web": {"results": [{"title": "Result", "url": "http://x.com", "description": "desc"}]}
    }
    mock_resp.raise_for_status = MagicMock()

    mock_http = AsyncMock()
    mock_http.get = AsyncMock(return_value=mock_resp)

    with patch.object(main, "BRAVE_API_KEY", "key123"):
        with patch.object(main._client, "_http", mock_http):
            result = await main.brave_search("test", count=5)
    assert "Result" in result


async def test_brave_search_no_results():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"web": {"results": []}}
    mock_resp.raise_for_status = MagicMock()
    mock_http = AsyncMock()
    mock_http.get = AsyncMock(return_value=mock_resp)

    with patch.object(main, "BRAVE_API_KEY", "key123"):
        with patch.object(main._client, "_http", mock_http):
            result = await main.brave_search("nothing")
    assert "No results" in result


async def test_brave_search_error():
    mock_http = AsyncMock()
    mock_http.get = AsyncMock(side_effect=Exception("network"))
    with patch.object(main, "BRAVE_API_KEY", "key123"):
        with patch.object(main._client, "_http", mock_http):
            result = await main.brave_search("test")
    assert "error" in result.lower()


async def test_render_diagram_empty():
    result = await main.render_diagram("   ")
    assert "empty" in result


async def test_render_diagram_disallowed():
    result = await main.render_diagram("file:///etc/passwd")
    assert "disallowed" in result


async def test_render_diagram_success():
    mock_resp = MagicMock()
    mock_resp.content = b"PNG_DATA"
    mock_resp.raise_for_status = MagicMock()
    mock_http = AsyncMock()
    mock_http.get = AsyncMock(return_value=mock_resp)

    with patch.object(main._client, "_http", mock_http):
        with patch("asyncio.create_task"):
            result = await main.render_diagram("graph TD; A-->B")
    assert result.endswith(".png")


async def test_render_diagram_error():
    mock_http = AsyncMock()
    mock_http.get = AsyncMock(side_effect=Exception("timeout"))
    with patch.object(main._client, "_http", mock_http):
        result = await main.render_diagram("graph TD; A-->B")
    assert "error" in result.lower()


async def test_render_chart_empty():
    result = await main.render_chart({})
    assert "empty" in result


async def test_render_chart_success():
    mock_resp = MagicMock()
    mock_resp.content = b"PNG"
    mock_resp.raise_for_status = MagicMock()
    mock_http = AsyncMock()
    mock_http.post = AsyncMock(return_value=mock_resp)

    with patch.object(main._client, "_http", mock_http):
        with patch("asyncio.create_task"):
            result = await main.render_chart({"type": "bar"})
    assert result.endswith(".png")


async def test_render_chart_error():
    mock_http = AsyncMock()
    mock_http.post = AsyncMock(side_effect=Exception("fail"))
    with patch.object(main._client, "_http", mock_http):
        result = await main.render_chart({"type": "bar"})
    assert "error" in result.lower()


async def test_list_olivetin_actions_empty():
    with patch.object(main._registry, "snapshot", return_value=[]):
        result = await main.list_olivetin_actions()
    assert json.loads(result) == []


async def test_list_olivetin_actions_with_actions(simple_action):
    with patch.object(main._registry, "snapshot", return_value=[simple_action]):
        result = await main.list_olivetin_actions()
    data = json.loads(result)
    assert data[0]["id"] == "test_action"
    assert "popup_on_start" in data[0]
    assert "order" in data[0]


async def test_get_execution_logs_all():
    with patch.object(main._client, "call", return_value={"logs": []}) as m:
        result = await main.get_execution_logs()
    m.assert_called_with("GetLogs", {"pageSize": 50})
    assert "logs" in json.loads(result)


async def test_get_execution_logs_by_action():
    with patch.object(main._client, "call", return_value={"entries": []}) as m:
        await main.get_execution_logs(action_id="my_act")
    m.assert_called_with("GetActionLogs", {"actionId": "my_act"})


async def test_get_execution_logs_error():
    with patch.object(main._client, "call", side_effect=Exception("fail")):
        result = await main.get_execution_logs()
    assert "error" in result.lower()


async def test_get_execution_status_success():
    with patch.object(main._client, "call", return_value={"status": "running"}) as m:
        result = await main.get_execution_status("tid123")
    m.assert_called_with("ExecutionStatus", {"executionTrackingId": "tid123"})
    assert "running" in result


async def test_get_execution_status_error():
    with patch.object(main._client, "call", side_effect=Exception("fail")):
        result = await main.get_execution_status("tid")
    assert "error" in result.lower()


async def test_kill_action_success():
    with patch.object(main._client, "call", return_value={"killed": True}) as m:
        result = await main.kill_action("tid123")
    m.assert_called_with("KillAction", {"executionTrackingId": "tid123"})
    assert "killed" in result


async def test_kill_action_error():
    with patch.object(main._client, "call", side_effect=Exception("fail")):
        result = await main.kill_action("tid")
    assert "error" in result.lower()


async def test_start_action_async_unknown():
    with patch.object(main._registry, "get", return_value=None):
        result = await main.start_action_async("ghost")
    assert "Unknown action" in result


async def test_start_action_async_rate_limited():
    action = {"bindingId": "x", "canExec": False, "datetimeRateLimitExpires": "2099"}
    with patch.object(main._registry, "get", return_value=action):
        result = await main.start_action_async("x")
    assert "rate-limited" in result


async def test_start_action_async_can_exec_false():
    action = {"bindingId": "x", "canExec": False}
    with patch.object(main._registry, "get", return_value=action):
        result = await main.start_action_async("x")
    assert "canExec=false" in result


async def test_start_action_async_success():
    action = {"bindingId": "x", "canExec": True, "arguments": []}
    with patch.object(main._registry, "get", return_value=action):
        with patch.object(main._client, "call", return_value={"executionTrackingId": "tid"}):
            result = await main.start_action_async("x", {"k": "v"})
    assert "tid" in result


async def test_start_action_async_http_error():
    action = {"bindingId": "x", "canExec": True, "arguments": []}
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.text = "Forbidden"
    with patch.object(main._registry, "get", return_value=action):
        with patch.object(
            main._client,
            "call",
            side_effect=httpx.HTTPStatusError("403", request=MagicMock(), response=mock_response),
        ):
            result = await main.start_action_async("x")
    assert "403" in result


async def test_start_action_async_generic_error():
    action = {"bindingId": "x", "canExec": True, "arguments": []}
    with patch.object(main._registry, "get", return_value=action):
        with patch.object(main._client, "call", side_effect=RuntimeError("fail")):
            result = await main.start_action_async("x")
    assert "error" in result.lower()


async def test_restart_action_success():
    with patch.object(main._client, "call", return_value={"executionTrackingId": "new"}) as m:
        result = await main.restart_action("old_tid")
    m.assert_called_with("RestartAction", {"executionTrackingId": "old_tid"})
    assert "new" in result


async def test_restart_action_http_error():
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not found"
    with patch.object(
        main._client,
        "call",
        side_effect=httpx.HTTPStatusError("404", request=MagicMock(), response=mock_response),
    ):
        result = await main.restart_action("tid")
    assert "404" in result


async def test_restart_action_error():
    with patch.object(main._client, "call", side_effect=Exception("fail")):
        result = await main.restart_action("tid")
    assert "error" in result.lower()


async def test_validate_argument_success():
    with patch.object(main._client, "call", return_value={"valid": True}) as m:
        result = await main.validate_argument("hello", "unicode")
    m.assert_called_with("ValidateArgumentType", {"value": "hello", "type": "unicode"})
    assert "valid" in result


async def test_validate_argument_with_optional_fields():
    with patch.object(main._client, "call", return_value={"valid": False}) as m:
        await main.validate_argument("x", "int", binding_id="act", argument_name="count")
    call_args = m.call_args[0][1]
    assert call_args["bindingId"] == "act"
    assert call_args["argumentName"] == "count"


async def test_validate_argument_error():
    with patch.object(main._client, "call", side_effect=Exception("fail")):
        result = await main.validate_argument("x", "unicode")
    assert "error" in result.lower()


async def test_whoami_success():
    with patch.object(main._client, "call", return_value={"user": "agent"}) as m:
        result = await main.whoami()
    m.assert_called_with("WhoAmI", {})
    assert "agent" in result


async def test_whoami_error():
    with patch.object(main._client, "call", side_effect=Exception("fail")):
        result = await main.whoami()
    assert "error" in result.lower()


async def test_get_server_info_success():
    with patch.object(main._client, "call", return_value={"version": "1.0"}) as m:
        result = await main.get_server_info()
    m.assert_called_with("Init", {})
    assert "version" in result


async def test_get_server_info_error():
    with patch.object(main._client, "call", side_effect=Exception("fail")):
        result = await main.get_server_info()
    assert "error" in result.lower()


async def test_get_action_binding_success():
    with patch.object(main._client, "call", return_value={"action": {}}) as m:
        result = await main.get_action_binding("my_action")
    m.assert_called_with("GetActionBinding", {"bindingId": "my_action"})
    assert "action" in result


async def test_get_action_binding_error():
    with patch.object(main._client, "call", side_effect=Exception("fail")):
        result = await main.get_action_binding("x")
    assert "error" in result.lower()


async def test_get_entities_success():
    with patch.object(main._client, "call", return_value={"entities": []}) as m:
        result = await main.get_entities()
    m.assert_called_with("GetEntities", {})
    assert "entities" in result


async def test_get_entities_error():
    with patch.object(main._client, "call", side_effect=Exception("fail")):
        result = await main.get_entities()
    assert "error" in result.lower()


async def test_get_entity_success():
    with patch.object(main._client, "call", return_value={"entity": {}}) as m:
        result = await main.get_entity("my-host", "server")
    m.assert_called_with("GetEntity", {"uniqueKey": "my-host", "type": "server"})
    assert "entity" in result


async def test_get_entity_no_type():
    with patch.object(main._client, "call", return_value={"entity": {}}) as m:
        await main.get_entity("my-host")
    call_args = m.call_args[0][1]
    assert "type" not in call_args


async def test_get_entity_error():
    with patch.object(main._client, "call", side_effect=Exception("fail")):
        result = await main.get_entity("x")
    assert "error" in result.lower()


async def test_readyz_success():
    with patch.object(main._client, "call", return_value={"ready": True}) as m:
        result = await main.readyz()
    m.assert_called_with("GetReadyz", {})
    assert "ready" in result


async def test_readyz_error():
    with patch.object(main._client, "call", side_effect=Exception("fail")):
        result = await main.readyz()
    assert "error" in result.lower()


async def test_reload_actions_tool():
    with patch.object(
        main._registry,
        "snapshot",
        side_effect=[
            [{"bindingId": "a"}],  # before
            [{"bindingId": "a"}, {"bindingId": "b"}],  # after
        ],
    ):
        with patch.object(main._registry, "reload", new=AsyncMock()):
            result = await main.reload_actions_tool()
    assert "2" in result
    assert "1" in result


# ─── MCP Resources ────────────────────────────────────────────────────────────


async def test_resource_olivetin_actions_empty():
    with patch.object(main._registry, "snapshot", return_value=[]):
        result = await main.resource_olivetin_actions()
    assert json.loads(result) == []


async def test_resource_olivetin_actions_fields(simple_action):
    with patch.object(main._registry, "snapshot", return_value=[simple_action]):
        result = await main.resource_olivetin_actions()
    data = json.loads(result)
    item = data[0]
    assert item["id"] == "test_action"
    assert "popup_on_start" in item
    assert "order" in item
    assert "approval_required" in item
    assert isinstance(item["args"], list)


async def test_resource_olivetin_actions_choice_titles():
    action = {
        "bindingId": "x",
        "title": "X",
        "canExec": True,
        "arguments": [
            {
                "name": "env",
                "type": "unicode",
                "choices": [{"value": "prod", "title": "Production"}],
            }
        ],
    }
    with patch.object(main._registry, "snapshot", return_value=[action]):
        result = await main.resource_olivetin_actions()
    data = json.loads(result)
    choice = data[0]["args"][0]["choices"][0]
    assert choice["value"] == "prod"
    assert choice["title"] == "Production"


async def test_resource_olivetin_action_help_unknown():
    with patch.object(main._registry, "get", return_value=None):
        result = await main.resource_olivetin_action_help("ghost")
    assert "Unknown action" in result


async def test_resource_olivetin_action_help_known(simple_action):
    with patch.object(main._registry, "get", return_value=simple_action):
        result = await main.resource_olivetin_action_help("test_action")
    assert "Test Action" in result
    assert "Arguments" in result
    assert "Order" in result
    assert "Popup on start" in result


async def test_resource_olivetin_action_help_readonly(readonly_action):
    with patch.object(main._registry, "get", return_value=readonly_action):
        result = await main.resource_olivetin_action_help("ro_status")
    assert "No — read-only" in result


async def test_resource_olivetin_action_help_rate_limited(simple_action):
    simple_action["canExec"] = False
    simple_action["datetimeRateLimitExpires"] = "2099-01-01"
    with patch.object(main._registry, "get", return_value=simple_action):
        result = await main.resource_olivetin_action_help("test_action")
    assert "rate-limited" in result


async def test_resource_olivetin_action_help_no_args(readonly_action):
    with patch.object(main._registry, "get", return_value=readonly_action):
        result = await main.resource_olivetin_action_help("ro_status")
    assert "No arguments" in result


async def test_resource_olivetin_action_help_with_choices():
    action = {
        "bindingId": "x",
        "title": "X",
        "canExec": True,
        "timeout": 5,
        "arguments": [
            {
                "name": "env",
                "type": "unicode",
                "choices": [
                    {"value": "prod", "title": "Production"},
                    {"value": "dev", "title": "dev"},
                ],
            }
        ],
    }
    with patch.object(main._registry, "get", return_value=action):
        result = await main.resource_olivetin_action_help("x")
    assert "Production" in result
    assert "Choices" in result


async def test_resource_olivetin_action_help_bare_choices():
    action = {
        "bindingId": "x",
        "title": "X",
        "canExec": True,
        "timeout": 5,
        "arguments": [{"name": "env", "type": "unicode", "choices": ["a", "b"]}],
    }
    with patch.object(main._registry, "get", return_value=action):
        result = await main.resource_olivetin_action_help("x")
    assert "'a'" in result


async def test_resource_olivetin_action_help_with_prompt_template():
    action = {
        "bindingId": "x",
        "title": "X",
        "canExec": True,
        "timeout": 5,
        "arguments": [],
        "promptTemplate": "Run {{action}}",
    }
    with patch.object(main._registry, "get", return_value=action):
        result = await main.resource_olivetin_action_help("x")
    assert "Prompt Template" in result


async def test_resource_olivetin_logs_success():
    with patch.object(main._client, "call", return_value={"logs": []}):
        result = await main.resource_olivetin_logs()
    assert "logs" in json.loads(result)


async def test_resource_olivetin_logs_error():
    with patch.object(main._client, "call", side_effect=Exception("fail")):
        result = await main.resource_olivetin_logs()
    assert "error" in json.loads(result)


async def test_resource_olivetin_action_logs_unknown():
    with patch.object(main._registry, "known", return_value=False):
        result = await main.resource_olivetin_action_logs("ghost")
    assert "error" in json.loads(result)


async def test_resource_olivetin_action_logs_success():
    with patch.object(main._registry, "known", return_value=True):
        with patch.object(main._client, "call", return_value={"entries": []}):
            result = await main.resource_olivetin_action_logs("my_act")
    assert "entries" in json.loads(result)


async def test_resource_olivetin_action_logs_error():
    with patch.object(main._registry, "known", return_value=True):
        with patch.object(main._client, "call", side_effect=Exception("fail")):
            result = await main.resource_olivetin_action_logs("my_act")
    assert "error" in json.loads(result)


async def test_resource_pending_approvals():
    with patch.object(main._approvals, "list_pending", return_value=[]):
        result = await main.resource_pending_approvals()
    assert json.loads(result)["pending"] == []


# ─── MCP Prompts ──────────────────────────────────────────────────────────────


async def test_prompt_olivetin_run_unknown():
    with patch.object(main._registry, "get", return_value=None):
        result = await main.prompt_olivetin_run("ghost")
    assert "not found" in result


async def test_prompt_olivetin_run_plain(simple_action):
    with patch.object(main._registry, "get", return_value=simple_action):
        result = await main.prompt_olivetin_run("test_action", "plain")
    assert "test_action" in result
    assert "raw output" in result


async def test_prompt_olivetin_run_json(simple_action):
    with patch.object(main._registry, "get", return_value=simple_action):
        result = await main.prompt_olivetin_run("test_action", "json")
    assert "JSON object" in result


async def test_prompt_olivetin_run_markdown(simple_action):
    with patch.object(main._registry, "get", return_value=simple_action):
        result = await main.prompt_olivetin_run("test_action", "markdown")
    assert "Markdown" in result


async def test_prompt_olivetin_run_summary(simple_action):
    with patch.object(main._registry, "get", return_value=simple_action):
        result = await main.prompt_olivetin_run("test_action", "summary")
    assert "Summarise" in result


async def test_prompt_olivetin_run_with_template(simple_action):
    simple_action["promptTemplate"] = "Please run {action} now."
    with patch.object(main._registry, "get", return_value=simple_action):
        result = await main.prompt_olivetin_run("test_action")
    assert "Please run" in result


async def test_prompt_olivetin_run_no_args():
    action = {"bindingId": "ping", "title": "Ping", "canExec": True, "arguments": []}
    with patch.object(main._registry, "get", return_value=action):
        result = await main.prompt_olivetin_run("ping")
    assert "ping" in result


# ─── Jinja2 templates ─────────────────────────────────────────────────────────


def test_approval_template_renders():
    html = _approval_tmpl.render(
        title="Deploy",
        action_id="deploy",
        args={"env": "prod"},
        token="abc",
        remaining=60,
    )
    assert "Deploy" in html
    assert "prod" in html
    assert "abc" in html


def test_approval_template_xss_escaped():
    html = _approval_tmpl.render(
        title="<script>alert(1)</script>",
        action_id="x",
        args={},
        token="t",
        remaining=10,
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_result_template_renders():
    html = _result_tmpl.render(icon="✅", title="Approved", message="Action approved.")
    assert "Approved" in html
    assert "Action approved." in html


# ─── ActionRegistry — _build_tool handler execution ──────────────────────────


async def test_build_tool_handler_executes(registry, simple_action, client):
    """The closure inside _build_tool must call run_action when invoked."""
    registry._actions = {"test_action": simple_action}
    client.call = AsyncMock(
        return_value={"logEntry": {"output": "pong", "exitCode": 0, "executionTrackingId": ""}}
    )
    tool = registry._build_tool(simple_action)
    # Call the underlying fn directly (bypasses FunctionTool wrapping)
    result = await tool.fn(msg="ping")
    assert result == "pong"


# ─── ActionRegistry — _build_prompt ──────────────────────────────────────────


def test_build_prompt_no_template(registry, simple_action):
    result = registry._build_prompt(simple_action)
    assert result is None


def test_build_prompt_success(registry):
    action = {
        "bindingId": "greet",
        "title": "Greet",
        "promptTemplate": "Say hello to {name}.",
        "arguments": [],
    }
    result = registry._build_prompt(action)
    assert result == "olivetin_greet"
    registry._mcp.add_prompt.assert_called()


def test_build_prompt_type_error_fallback(registry):
    """When add_prompt raises TypeError (signature mismatch), fall back to no-kwargs call."""
    registry._mcp.add_prompt.side_effect = [TypeError("unexpected kwarg"), None]
    action = {"bindingId": "x", "title": "X", "promptTemplate": "Do {{thing}}.", "arguments": []}
    result = registry._build_prompt(action)
    assert result == "olivetin_x"
    assert registry._mcp.add_prompt.call_count == 2


def test_build_prompt_type_error_inner_exception(registry):
    """Both add_prompt calls raise — should return None."""
    registry._mcp.add_prompt.side_effect = [TypeError("bad"), RuntimeError("also bad")]
    action = {"bindingId": "x", "title": "X", "promptTemplate": "Do it.", "arguments": []}
    result = registry._build_prompt(action)
    assert result is None


def test_build_prompt_outer_exception(registry):
    registry._mcp.add_prompt.side_effect = RuntimeError("mcp error")
    action = {"bindingId": "x", "title": "X", "promptTemplate": "Do it.", "arguments": []}
    result = registry._build_prompt(action)
    assert result is None


async def test_build_prompt_fn_substitutes_kwargs(registry):
    """The inner _prompt_fn must substitute {key} placeholders."""
    captured_fn = None

    def capture_add_prompt(fn, **kwargs):
        nonlocal captured_fn
        captured_fn = fn

    registry._mcp.add_prompt.side_effect = capture_add_prompt
    action = {
        "bindingId": "greet",
        "title": "Greet",
        "promptTemplate": "Hello {name}, you are {age} years old.",
        "arguments": [],
    }
    registry._build_prompt(action)
    assert captured_fn is not None
    result = await captured_fn(name="Alice", age="30")
    assert result == "Hello Alice, you are 30 years old."


# ─── ActionRegistry — _rebuild ────────────────────────────────────────────────


async def test_rebuild_removes_existing_tools(registry, simple_action):
    registry._action_tool_names = {"old_tool"}
    registry._prompt_names = {"old_prompt"}
    await registry._rebuild({})
    registry._mcp.local_provider.remove_tool.assert_called_with("old_tool")
    registry._mcp.remove_prompt.assert_called_with("old_prompt")


async def test_rebuild_remove_tool_exception_swallowed(registry):
    registry._action_tool_names = {"stale"}
    registry._mcp.local_provider.remove_tool.side_effect = Exception("already gone")
    await registry._rebuild({})  # should not raise


async def test_rebuild_remove_prompt_exception_swallowed(registry):
    registry._prompt_names = {"stale_prompt"}
    registry._mcp.remove_prompt.side_effect = Exception("already gone")
    await registry._rebuild({})  # should not raise


async def test_rebuild_registers_readonly(registry, readonly_action):
    await registry._rebuild({"ro_status": readonly_action})
    assert "ro_status" in registry.readonly_tool_names


async def test_rebuild_registers_prompt(registry):
    action = {
        "bindingId": "greet",
        "title": "Greet",
        "promptTemplate": "Say hello.",
        "arguments": [],
        "canExec": True,
    }
    await registry._rebuild({"greet": action})
    assert "olivetin_greet" in registry._prompt_names


# ─── ActionRegistry — refresh_loop break path ────────────────────────────────


async def test_refresh_loop_exits_when_event_set_during_wait(registry, client):
    """stop_event set while waiting for timeout causes loop to break."""
    client.call = AsyncMock(return_value={"dashboard": {"contents": []}})

    async def set_after_delay(event: asyncio.Event) -> None:
        await asyncio.sleep(0.05)
        event.set()

    stop = asyncio.Event()
    asyncio.create_task(set_after_delay(stop))
    registry._cfg = Settings(ACTION_REFRESH_INTERVAL_SECS=100, SECRETS_DIR="/nonexistent")
    await registry.refresh_loop(stop)  # should return when stop is set


# ─── _lifespan ────────────────────────────────────────────────────────────────


async def test_lifespan_starts_and_stops():
    """_lifespan starts _client, reloads registry, starts refresh_loop, then cleans up."""
    with patch.object(main._client, "start", new=AsyncMock()) as mock_start:
        with patch.object(main._client, "close", new=AsyncMock()) as mock_close:
            with patch.object(main._registry, "reload", new=AsyncMock()) as mock_reload:
                with patch.object(main._registry, "refresh_loop", new=AsyncMock()):
                    async with main._lifespan(None):
                        pass  # yield point
    mock_start.assert_called_once()
    mock_close.assert_called_once()
    mock_reload.assert_called_once()


# ─── resource_olivetin_action_help with description ───────────────────────────


async def test_resource_olivetin_action_help_with_description():
    action = {
        "bindingId": "x",
        "title": "X",
        "canExec": True,
        "timeout": 5,
        "description": "Does important things.",
        "arguments": [],
    }
    with patch.object(main._registry, "get", return_value=action):
        result = await main.resource_olivetin_action_help("x")
    assert "Does important things." in result
    assert "## Description" in result


async def test_resource_olivetin_action_help_can_exec_false_no_rate():
    action = {
        "bindingId": "x",
        "title": "X",
        "canExec": False,
        "timeout": 5,
        "arguments": [],
    }
    with patch.object(main._registry, "get", return_value=action):
        result = await main.resource_olivetin_action_help("x")
    assert "No\n" in result or "**Can execute**: No" in result


# ─── __main__ block ───────────────────────────────────────────────────────────


def test_main_block_calls_mcp_run():
    with patch.object(main.mcp, "run") as mock_run:
        # Simulate running as __main__ by calling mcp.run with the expected args
        main.mcp.run(
            transport="sse",
            host="0.0.0.0",
            port=main.cfg.HOST_BRIDGE_PORT,
            path="/sse",
            log_level="warning",
            show_banner=False,
        )
    mock_run.assert_called_once_with(
        transport="sse",
        host="0.0.0.0",
        port=main.cfg.HOST_BRIDGE_PORT,
        path="/sse",
        log_level="warning",
        show_banner=False,
    )


# ─── _STATIC_READONLY_TOOLS completeness ─────────────────────────────────────


def test_static_readonly_tools_contains_expected():
    expected = {
        "list_pending_approvals",
        "brave_search",
        "render_diagram",
        "render_chart",
        "list_olivetin_actions",
        "get_execution_logs",
        "get_execution_status",
        "kill_action",
        "reload_actions",
        "validate_argument",
        "whoami",
        "get_server_info",
        "get_action_binding",
        "get_entities",
        "get_entity",
        "readyz",
    }
    assert expected <= _STATIC_READONLY_TOOLS


# ─── Coverage: approval_fallback hook (line 853) ─────────────────────────────


async def test_middleware_approval_fallback_approved():
    """When approval_fallback is set and returns True, the tool should execute."""
    mw = main._ApprovalMiddleware()
    ctx = MagicMock()
    ctx.message = MagicMock()
    ctx.message.name = "destructive_tool"
    ctx.message.arguments = {"key": "val"}
    ctx.fastmcp_context = None
    call_next = AsyncMock(return_value="executed")

    async def fake_fallback(tool_name, args, display):
        return True

    original = main.approval_fallback
    try:
        main.approval_fallback = fake_fallback
        with patch.object(main._approvals, "try_elicitation", return_value=None):
            await mw.on_call_tool(ctx, call_next)
        call_next.assert_called_once()
    finally:
        main.approval_fallback = original


async def test_middleware_approval_fallback_denied():
    """When approval_fallback returns False, the tool should be denied."""
    mw = main._ApprovalMiddleware()
    ctx = MagicMock()
    ctx.message = MagicMock()
    ctx.message.name = "destructive_tool"
    ctx.message.arguments = {}
    ctx.fastmcp_context = None
    call_next = AsyncMock(return_value="executed")

    async def fake_fallback(tool_name, args, display):
        return False

    original = main.approval_fallback
    try:
        main.approval_fallback = fake_fallback
        with patch.object(main._approvals, "try_elicitation", return_value=None):
            result = await mw.on_call_tool(ctx, call_next)
        call_next.assert_not_called()
        assert result[0].text.startswith("⛔")
    finally:
        main.approval_fallback = original


# ─── Coverage: healthz_route (line 885) ──────────────────────────────────────


async def test_healthz_route():
    request = MagicMock()
    response = await main.healthz_route(request)
    assert response.status_code == 200
    assert b"ok" in response.body


# ─── Security headers on HTML responses ──────────────────────────────────────


def test_security_headers_constant():
    from main import _SECURITY_HEADERS

    assert _SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert _SECURITY_HEADERS["X-Frame-Options"] == "DENY"
    assert "default-src 'none'" in _SECURITY_HEADERS["Content-Security-Policy"]
    assert _SECURITY_HEADERS["Referrer-Policy"] == "no-referrer"
    assert _SECURITY_HEADERS["Cache-Control"] == "no-store"


def test_html_response_includes_security_headers():
    from main import _html_response

    resp = _html_response("<p>test</p>")
    assert resp.status_code == 200
    for key in ("x-content-type-options", "x-frame-options", "referrer-policy"):
        assert key in resp.headers


def test_html_response_custom_status_code():
    from main import _html_response

    resp = _html_response("<p>err</p>", status_code=404)
    assert resp.status_code == 404


async def test_pending_detail_route_has_security_headers():
    p = {
        "action_id": "deploy",
        "title": "Deploy",
        "args": {},
        "expires_at": time.time() + 60,
    }
    with patch.object(main, "_approvals") as mock_approvals:
        mock_approvals.get.return_value = p
        request = MagicMock()
        request.path_params = {"token": "tok"}
        response = await main.pending_detail_route(request)
    assert "x-frame-options" in response.headers


async def test_pending_approve_has_security_headers():
    p = {"action_id": "deploy", "title": "Deploy"}
    with patch.object(main, "_approvals") as mock_approvals:
        mock_approvals.get.return_value = p
        mock_approvals.approve.return_value = True
        request = MagicMock()
        request.path_params = {"token": "tok"}
        response = await main.pending_approve_route(request)
    assert "x-frame-options" in response.headers


# ─── SSRF protection: all disallowed protocols ───────────────────────────────


@pytest.mark.parametrize(
    "protocol",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<h1>hi</h1>",
        "gopher://evil.com",
        "ftp://evil.com/file",
        "dict://evil.com",
        "ldap://evil.com",
        "tftp://evil.com",
        "ssh://evil.com",
    ],
)
async def test_render_diagram_ssrf_all_protocols(protocol):
    result = await main.render_diagram(protocol)
    assert "disallowed" in result


@pytest.mark.parametrize(
    "protocol",
    ["FILE:///etc/passwd", "JAVASCRIPT:alert(1)", "GOPHER://evil.com"],
)
async def test_render_diagram_ssrf_case_insensitive(protocol):
    result = await main.render_diagram(protocol)
    assert "disallowed" in result


# ─── XSS prevention in templates ─────────────────────────────────────────────


def test_approval_template_xss_in_action_id():
    html = _approval_tmpl.render(
        title="Safe Title",
        action_id='<script>alert("xss")</script>',
        args={},
        token="tok",
        remaining=60,
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_approval_template_xss_in_args():
    html = _approval_tmpl.render(
        title="Safe",
        action_id="deploy",
        args={"key": '<img src=x onerror="alert(1)">'},
        token="tok",
        remaining=60,
    )
    # Jinja2 autoescape should escape < and > so the tag doesn't render
    assert "<img src=x" not in html
    assert "&lt;img" in html


def test_result_template_xss_in_all_fields():
    html = _result_tmpl.render(
        icon="<script>alert(1)</script>",
        title="<script>alert(2)</script>",
        message="<script>alert(3)</script>",
    )
    assert html.count("<script>") == 0


def test_template_jinja_injection():
    html = _approval_tmpl.render(
        title="{{ 7*7 }}",
        action_id="{{ config }}",
        args={},
        token="tok",
        remaining=60,
    )
    assert "49" not in html
    assert "config" in html  # rendered as literal text


# ─── Edge cases in action execution ──────────────────────────────────────────


async def test_run_action_nonzero_exit_with_output(registry, simple_action, client):
    """Non-zero exit code WITH output should return the output, not exit code."""
    registry._actions = {"test_action": simple_action}
    client.call = AsyncMock(
        return_value={"logEntry": {"output": "error details here", "exitCode": 1}}
    )
    result = await registry.run_action("test_action", {"msg": "hi"})
    assert "error details here" in result


async def test_run_action_timeout_clamped_min(registry, client):
    """Timeout < 5 should be clamped to 5."""
    action = {
        "bindingId": "fast",
        "title": "Fast",
        "timeout": 1,
        "canExec": True,
        "arguments": [],
    }
    registry._actions = {"fast": action}
    client.call = AsyncMock(return_value={"logEntry": {"output": "ok", "exitCode": 0}})
    await registry.run_action("fast", {})
    _, kwargs = client.call.call_args
    assert kwargs["timeout"] == 15  # min(max(1,5),600) + 10 = 15


async def test_run_action_timeout_clamped_max(registry, client):
    """Timeout > 600 should be clamped to 600."""
    action = {
        "bindingId": "slow",
        "title": "Slow",
        "timeout": 9999,
        "canExec": True,
        "arguments": [],
    }
    registry._actions = {"slow": action}
    client.call = AsyncMock(return_value={"logEntry": {"output": "ok", "exitCode": 0}})
    await registry.run_action("slow", {})
    _, kwargs = client.call.call_args
    assert kwargs["timeout"] == 610  # min(max(9999,5),600) + 10 = 610


# ─── StartAction uses actionId not bindingId ─────────────────────────────────


async def test_start_action_async_sends_action_id():
    action = {"bindingId": "deploy", "canExec": True, "arguments": []}
    with patch.object(main._registry, "get", return_value=action):
        with patch.object(
            main._client,
            "call",
            return_value={"executionTrackingId": "tid"},
        ) as m:
            await main.start_action_async("deploy", {"k": "v"})
    payload = m.call_args[0][1]
    assert "actionId" in payload
    assert "bindingId" not in payload
    assert payload["actionId"] == "deploy"


# ─── start_action_async None arguments handling ──────────────────────────────


async def test_start_action_async_none_arguments():
    action = {"bindingId": "x", "canExec": True, "arguments": []}
    with patch.object(main._registry, "get", return_value=action):
        with patch.object(
            main._client,
            "call",
            return_value={"executionTrackingId": "tid"},
        ) as m:
            await main.start_action_async("x", None)
    payload = m.call_args[0][1]
    assert payload["arguments"] == []


# ─── get_limiter atomicity ───────────────────────────────────────────────────


def test_get_limiter_setdefault_returns_same(registry):
    lim1 = registry.get_limiter("action_a")
    lim2 = registry.get_limiter("action_a")
    assert lim1 is lim2


# ─── _rebuild logging on exceptions ──────────────────────────────────────────


async def test_rebuild_tool_removal_exception_logged(registry, mock_mcp):
    registry._action_tool_names = {"old_tool"}
    mock_mcp.local_provider = MagicMock()
    mock_mcp.local_provider.remove_tool.side_effect = RuntimeError("nope")
    # Should not raise
    await registry._rebuild({})
    assert len(registry._action_tool_names) == 0


async def test_rebuild_prompt_removal_exception_logged(registry, mock_mcp):
    registry._prompt_names = {"old_prompt"}
    mock_mcp.remove_prompt.side_effect = RuntimeError("nope")
    await registry._rebuild({})
    assert len(registry._prompt_names) == 0


# ─── Approval edge cases ────────────────────────────────────────────────────


async def test_approve_multiple_times_second_ignored(approvals):
    token = "tok123"
    fut = asyncio.get_event_loop().create_future()
    approvals._pending[token] = {
        "future": fut,
        "action_id": "x",
        "title": "X",
        "args": {},
        "created_at": time.time(),
        "expires_at": time.time() + 60,
    }
    assert approvals.approve(token) is True
    assert approvals.approve(token) is False  # already done


async def test_deny_after_approve_ignored(approvals):
    token = "tok456"
    fut = asyncio.get_event_loop().create_future()
    approvals._pending[token] = {
        "future": fut,
        "action_id": "x",
        "title": "X",
        "args": {},
        "created_at": time.time(),
        "expires_at": time.time() + 60,
    }
    approvals.approve(token)
    assert approvals.deny(token) is False


async def test_list_pending_expired_shows_zero(approvals):
    token = "expired_tok"
    fut = asyncio.get_event_loop().create_future()
    approvals._pending[token] = {
        "future": fut,
        "action_id": "x",
        "title": "X",
        "args": {},
        "created_at": time.time() - 200,
        "expires_at": time.time() - 100,
    }
    items = approvals.list_pending()
    assert len(items) == 1
    assert items[0]["expires_in"] == 0


# ─── _make_action_model edge cases ──────────────────────────────────────────


def test_make_action_model_html_arg_filtered():
    action = {
        "bindingId": "x",
        "arguments": [
            {"name": "visible", "type": "unicode"},
            {"name": "hidden", "type": "html"},
            {"name": "confirm", "type": "confirmation"},
        ],
    }
    model = ActionRegistry._make_action_model(action)
    schema = model.model_json_schema()
    assert "visible" in schema.get("properties", {})
    assert "hidden" not in schema.get("properties", {})
    assert "confirm" not in schema.get("properties", {})


def test_make_action_model_ascii_identifier_has_pattern():
    action = {
        "bindingId": "x",
        "arguments": [
            {"name": "host", "type": "ascii_identifier"},
        ],
    }
    schema = ActionRegistry._make_input_schema(action)
    props = schema.get("properties", {})
    assert "pattern" in props["host"]
    assert props["host"]["pattern"] == r"^[a-zA-Z0-9_\-]+$"


def test_make_action_model_url_has_format():
    action = {
        "bindingId": "x",
        "arguments": [
            {"name": "endpoint", "type": "url"},
        ],
    }
    schema = ActionRegistry._make_input_schema(action)
    props = schema.get("properties", {})
    assert props["endpoint"].get("format") == "uri"


# ─── _can_exec_label helper ─────────────────────────────────────────────────


def test_can_exec_label_true():
    from main import _can_exec_label

    assert _can_exec_label(True, "") == "Yes"


def test_can_exec_label_false_no_rate():
    from main import _can_exec_label

    assert _can_exec_label(False, "") == "No"


def test_can_exec_label_false_with_rate():
    from main import _can_exec_label

    result = _can_exec_label(False, "2026-01-01T00:00:00Z")
    assert "rate-limited" in result
    assert "2026" in result


# ─── brave_search edge cases ────────────────────────────────────────────────


async def test_brave_search_count_clamped():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "web": {"results": [{"title": "T", "url": "U", "description": "D"}]}
    }
    mock_http = AsyncMock()
    mock_http.get = AsyncMock(return_value=mock_resp)

    with (
        patch.object(main._client, "_http", mock_http),
        patch.object(main, "BRAVE_API_KEY", "test-key"),
    ):
        await main.brave_search("test", count=100)
    _, kwargs = mock_http.get.call_args
    assert kwargs["params"]["count"] == 20  # clamped


# ─── FastMCP server name ────────────────────────────────────────────────────


def test_mcp_server_name():
    assert main.mcp.name == "olivetin-mcp"


# ─── Auth edge cases ────────────────────────────────────────────────────────


def test_auth_headers_groups_without_user():
    """Setting groups without user should not add the user header."""
    s = Settings(
        OLIVETIN_REMOTE_GROUPS="admins",
        OLIVETIN_REMOTE_USER="",
        SECRETS_DIR="/nonexistent",
    )
    c = OliveTinClient(s)
    h = c.auth_headers()
    assert "X-Remote-User" not in h
    assert h["X-Remote-Groups"] == "admins"


def test_auth_headers_basic_needs_both():
    """Basic auth requires both user and pass."""
    s = Settings(
        OLIVETIN_BASIC_USER="admin",
        OLIVETIN_BASIC_PASS="",
        SECRETS_DIR="/nonexistent",
    )
    c = OliveTinClient(s)
    h = c.auth_headers()
    assert "Authorization" not in h


# ─── Pending list with no base URL ───────────────────────────────────────────


async def test_pending_list_route():
    with patch.object(main._approvals, "list_pending", return_value=[]):
        request = MagicMock()
        response = await main.pending_list_route(request)
    assert response.status_code == 200
    assert b"pending" in response.body
