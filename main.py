#!/usr/bin/env python3
"""
claw-host-bridge — OliveTin MCP wrapper with first-class human approval
────────────────────────────────────────────────────────────────────────
Approval chain (built-in, no external proxy required):
  1. MCP native elicitation  — inline dialog in Claude Code / Claude Desktop
  2. URL-based pending        — human visits /pending/<token> to approve or deny

OliveTin proto: https://github.com/OliveTin/OliveTin/blob/main/proto/olivetin/api/v1/olivetin.proto

Design:
  OliveTinClient   — httpx connection pool, auth headers, tenacity retry
  ActionRegistry   — action catalog, dynamic MCP tools/prompts, rate limiters
  ApprovalManager  — pending approval queue, elicitation, URL-based pending
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import mcp.types as mt
import structlog
from aiolimiter import AsyncLimiter
from fastmcp import FastMCP
from fastmcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
)
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.function_tool import FunctionTool
from jinja2 import BaseLoader, Environment
from pydantic import Field, create_model
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# ─── Structured logging ───────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer(colors=False),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)
log: structlog.BoundLogger = structlog.get_logger("host-bridge")


# ─── Settings ─────────────────────────────────────────────────────────────────


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_ignore_empty=True, case_sensitive=True)

    HOST_BRIDGE_PORT: int = 9003
    OLIVETIN_URL: str = "http://host.docker.internal:1337"
    APPROVAL_TIMEOUT_SECS: int = 120
    # Public-facing URL of this bridge so humans can click approval links.
    # Example: http://192.168.1.10:9003  or  https://bridge.example.com
    APPROVAL_BASE_URL: str = ""
    # Seconds to wait for the MCP client to respond to an elicitation dialog
    # before falling through to URL-based pending.
    ELICITATION_TIMEOUT_SECS: float = 30.0
    SECRETS_DIR: str = "/run/secrets"
    # How often to refresh the OliveTin action catalog (seconds).
    ACTION_REFRESH_INTERVAL_SECS: int = 300

    # ── OliveTin auth ──────────────────────────────────────────────────────────
    # OliveTin supports three auth modes. Set the relevant vars for your setup.
    #
    # 1. HTTP header auth (reverse proxy: Authelia, Traefik, nginx auth_request)
    #    OliveTin trusts X-Remote-User / X-Remote-Groups injected by the proxy.
    #    Set OLIVETIN_REMOTE_USER to identify this bridge as a named service account.
    #    Set OLIVETIN_REMOTE_GROUPS to a comma-separated list of groups.
    #    Example: OLIVETIN_REMOTE_USER=claude-agent  OLIVETIN_REMOTE_GROUPS=admins
    OLIVETIN_REMOTE_USER: str = ""
    OLIVETIN_REMOTE_GROUPS: str = ""
    # Custom header names if your OliveTin is configured with non-default headers.
    OLIVETIN_USER_HEADER: str = "X-Remote-User"
    OLIVETIN_GROUPS_HEADER: str = "X-Remote-Groups"

    # 2. JWT bearer token auth
    #    Set OLIVETIN_JWT_TOKEN (or drop the value in /run/secrets/olivetin_jwt_token).
    OLIVETIN_JWT_TOKEN: str = ""

    # 3. Basic auth (less common, but supported by some OliveTin reverse proxy setups)
    OLIVETIN_BASIC_USER: str = ""
    OLIVETIN_BASIC_PASS: str = ""


cfg = Settings()


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _read_secret(name: str) -> str:
    for candidate in [name, name.split(".")[0]]:
        path = os.path.join(cfg.SECRETS_DIR, candidate)
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
    return ""


BRAVE_API_KEY = _read_secret("brave_api_key")

_OLIVETIN_TYPE_MAP: dict[str, type] = {
    "unicode": str,
    "ascii_identifier": str,
    "url": str,
    "int": int,
    "float": float,
    "bool": bool,
}

# Static tools registered by @mcp.tool decorators — always read-only.
_STATIC_READONLY_TOOLS = frozenset(
    {
        "list_pending_approvals",
        "brave_search",
        "render_diagram",
        "render_chart",
        "list_olivetin_actions",
        "get_execution_logs",
        "get_execution_status",
        "kill_action",
        "reload_actions",
        # OliveTin RPC read-only tools
        "validate_argument",
        "whoami",
        "get_server_info",
        "get_action_binding",
        "get_entities",
        "get_entity",
        "readyz",
    }
)


# ─── HTML templates ───────────────────────────────────────────────────────────
# autoescape=True prevents XSS from user-supplied values (action names, args).

_jinja = Environment(loader=BaseLoader(), autoescape=True)

_APPROVAL_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Approval Required — {{ title }}</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: #0f172a; color: #e2e8f0; min-height: 100vh;
           display: flex; align-items: center; justify-content: center; padding: 20px; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px;
            max-width: 540px; width: 100%; padding: 32px; }
    h1 { font-size: 1.4rem; color: #f8fafc; margin-bottom: 6px; }
    .sub { color: #94a3b8; font-size: 0.85rem; margin-bottom: 24px; }
    .badge { display: inline-block; background: #dc2626; color: #fff;
             font-size: 0.7rem; font-weight: 700; padding: 2px 8px;
             border-radius: 4px; letter-spacing: 0.05em; margin-bottom: 16px; }
    .action-box { background: #0f172a; border: 1px solid #475569;
                  border-radius: 8px; padding: 16px; margin-bottom: 20px; }
    .action-id { font-family: monospace; font-size: 0.85rem; color: #7dd3fc; margin-bottom: 8px; }
    .arg-row { display: flex; gap: 8px; padding: 4px 0;
               border-bottom: 1px solid #1e293b; font-size: 0.85rem; }
    .arg-row:last-child { border-bottom: none; }
    .arg-name { color: #94a3b8; min-width: 120px; flex-shrink: 0; }
    .arg-val { color: #e2e8f0; word-break: break-all; }
    .timer { font-size: 0.8rem; color: #f59e0b; margin-bottom: 20px; }
    .buttons { display: flex; gap: 12px; }
    form { flex: 1; }
    button { width: 100%; padding: 12px; font-size: 1rem; font-weight: 600;
             border: none; border-radius: 8px; cursor: pointer; transition: opacity .15s; }
    button:hover { opacity: 0.85; }
    .btn-approve { background: #16a34a; color: #fff; }
    .btn-deny    { background: #dc2626; color: #fff; }
  </style>
</head>
<body>
  <div class="card">
    <span class="badge">APPROVAL REQUIRED</span>
    <h1>{{ title }}</h1>
    <p class="sub">An agent is waiting for your decision.</p>
    <div class="action-box">
      <div class="action-id">{{ action_id }}</div>
      {% if args %}
      <div style="margin-top:8px">
        {% for k, v in args.items() %}
        <div class="arg-row">
          <span class="arg-name">{{ k }}</span>
          <span class="arg-val">{{ v }}</span>
        </div>
        {% endfor %}
      </div>
      {% endif %}
    </div>
    <p class="timer">Expires in ~{{ remaining }}s</p>
    <div class="buttons">
      <form method="POST" action="/pending/{{ token }}/approve">
        <button class="btn-approve" type="submit">&#x2705; Approve</button>
      </form>
      <form method="POST" action="/pending/{{ token }}/deny">
        <button class="btn-deny" type="submit">&#x274C; Deny</button>
      </form>
    </div>
  </div>
</body>
</html>
"""

_RESULT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{{ title }}</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: #0f172a; color: #e2e8f0; min-height: 100vh;
           display: flex; align-items: center; justify-content: center; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px;
            max-width: 420px; padding: 40px; text-align: center; }
    .icon { font-size: 3rem; margin-bottom: 16px; }
    h1 { font-size: 1.4rem; color: #f8fafc; }
    p { color: #94a3b8; margin-top: 8px; font-size: 0.9rem; }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{{ icon }}</div>
    <h1>{{ title }}</h1>
    <p>{{ message }}</p>
  </div>
</body>
</html>
"""

_approval_tmpl = _jinja.from_string(_APPROVAL_TEMPLATE)
_result_tmpl = _jinja.from_string(_RESULT_TEMPLATE)


# ─── OliveTinClient ───────────────────────────────────────────────────────────
# Owns the httpx connection pool, auth header construction, and the tenacity
# retry wrapper. All OliveTin API calls go through client.call().
#
# Supported auth modes (all optional, compose freely):
#   1. HTTP header auth  — OLIVETIN_REMOTE_USER / OLIVETIN_REMOTE_GROUPS
#   2. JWT bearer        — OLIVETIN_JWT_TOKEN (or /run/secrets/olivetin_jwt_token)
#   3. HTTP Basic        — OLIVETIN_BASIC_USER / OLIVETIN_BASIC_PASS
#
# REST-transcoded gRPC endpoints (all POST, JSON body):
#   GetDashboard, StartActionAndWait, StartAction, RestartAction,
#   GetLogs, GetActionLogs, ExecutionStatus, KillAction,
#   ValidateArgumentType, WhoAmI, Init, GetActionBinding,
#   GetEntities, GetEntity, GetReadyz


class OliveTinClient:
    def __init__(self, settings: Settings) -> None:
        self._cfg = settings
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=620.0, write=10.0, pool=10.0),
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    def auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._cfg.OLIVETIN_REMOTE_USER:
            headers[self._cfg.OLIVETIN_USER_HEADER] = self._cfg.OLIVETIN_REMOTE_USER
        if self._cfg.OLIVETIN_REMOTE_GROUPS:
            headers[self._cfg.OLIVETIN_GROUPS_HEADER] = self._cfg.OLIVETIN_REMOTE_GROUPS
        jwt = self._cfg.OLIVETIN_JWT_TOKEN or _read_secret("olivetin_jwt_token")
        if jwt:
            headers["Authorization"] = f"Bearer {jwt}"
        if self._cfg.OLIVETIN_BASIC_USER and self._cfg.OLIVETIN_BASIC_PASS:
            creds = base64.b64encode(
                f"{self._cfg.OLIVETIN_BASIC_USER}:{self._cfg.OLIVETIN_BASIC_PASS}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {creds}"
        return headers

    def active_auth_modes(self) -> str:
        modes = []
        if self._cfg.OLIVETIN_REMOTE_USER:
            modes.append(
                f"header({self._cfg.OLIVETIN_USER_HEADER}={self._cfg.OLIVETIN_REMOTE_USER})"
            )
        if self._cfg.OLIVETIN_JWT_TOKEN or _read_secret("olivetin_jwt_token"):
            modes.append("jwt")
        if self._cfg.OLIVETIN_BASIC_USER:
            modes.append(f"basic({self._cfg.OLIVETIN_BASIC_USER})")
        return ", ".join(modes) if modes else "none"

    @retry(
        retry=retry_if_exception_type(httpx.RequestError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
        reraise=True,
    )
    async def call(self, endpoint: str, payload: dict, timeout: int = 30) -> dict:
        """Call an OliveTin REST API endpoint. Retries up to 3× on transient network errors."""
        assert self._http is not None, "HTTP client not initialised — call start() first"
        resp = await self._http.post(
            f"{self._cfg.OLIVETIN_URL}/api/{endpoint}",
            json=payload,
            headers=self.auth_headers(),
            timeout=timeout + 5,
        )
        resp.raise_for_status()
        return resp.json()


# ─── ActionRegistry ───────────────────────────────────────────────────────────
# Owns the OliveTin action catalog and all dynamic MCP state derived from it:
# tools, prompts, rate limiters, read-only classification.
#
# Call reload() on startup and periodically; it fetches GetDashboard, diffs the
# catalog, removes stale tools/prompts, and registers fresh ones via mcp.add_tool.


class ActionRegistry:
    def __init__(self, mcp: FastMCP, settings: Settings, client: OliveTinClient) -> None:
        self._mcp = mcp
        self._cfg = settings
        self._client = client
        self._actions: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._action_tool_names: set[str] = set()
        self._prompt_names: set[str] = set()
        self._limiters: dict[str, AsyncLimiter] = {}
        # Kept public so _ApprovalMiddleware can read it without coupling to internals.
        self.readonly_tool_names: set[str] = set(_STATIC_READONLY_TOOLS)

    # ── Catalog access ────────────────────────────────────────────────────────

    async def get(self, action_id: str) -> dict | None:
        async with self._lock:
            return self._actions.get(action_id)

    async def snapshot(self) -> list[dict]:
        async with self._lock:
            return list(self._actions.values())

    async def known(self, action_id: str) -> bool:
        async with self._lock:
            return action_id in self._actions

    def get_limiter(self, action_id: str) -> AsyncLimiter:
        if action_id not in self._limiters:
            self._limiters[action_id] = AsyncLimiter(max_rate=2, time_period=1.0)
        return self._limiters[action_id]

    # ── Classification helpers ────────────────────────────────────────────────

    @staticmethod
    def is_read_only(action: dict) -> bool:
        """Convention: '[read-only]' title prefix or 'ro_' bindingId prefix bypasses approval."""
        title = action.get("title", "").lower()
        bid = action.get("bindingId", "").lower()
        return title.startswith("[read-only]") or bid.startswith("ro_")

    @staticmethod
    def clean_title(action: dict) -> str:
        return (
            action.get("title", action.get("bindingId", ""))
            .replace("[read-only]", "")
            .replace("[READ-ONLY]", "")
            .strip()
        )

    def display_name(self, tool_name: str) -> str:
        action = self._actions.get(tool_name)
        return self.clean_title(action) if action else tool_name.replace("_", " ").title()

    # ── Schema / tool building ────────────────────────────────────────────────

    @staticmethod
    def _make_action_model(action: dict) -> type:
        fields: dict[str, Any] = {}
        for arg in action.get("arguments", []):
            arg_type = arg.get("type", "unicode")
            if arg_type in ("html", "confirmation"):
                continue
            name = arg["name"]
            py_type = _OLIVETIN_TYPE_MAP.get(arg_type, str)
            choices = [c["value"] if isinstance(c, dict) else c for c in arg.get("choices", [])]
            default = arg.get("defaultValue", "") or arg.get("default", "")
            desc = arg.get("description") or arg.get("title") or None
            field_kwargs: dict[str, Any] = {}
            if desc:
                field_kwargs["description"] = desc
            if arg_type == "ascii_identifier":
                field_kwargs["pattern"] = r"^[a-zA-Z0-9_\-]+$"
            schema_extra: dict[str, Any] = {}
            if choices:
                schema_extra["enum"] = choices
            if arg_type == "url":
                schema_extra["format"] = "uri"
            if schema_extra:
                field_kwargs["json_schema_extra"] = schema_extra
            if default:
                fields[name] = (py_type, Field(default=default, **field_kwargs))
            else:
                fields[name] = (py_type, Field(..., **field_kwargs))
        model_id = re.sub(r"[^a-zA-Z0-9_]", "_", action["bindingId"])
        return create_model(f"Action_{model_id}", **fields)

    @classmethod
    def _make_input_schema(cls, action: dict) -> dict:
        schema = cls._make_action_model(action).model_json_schema()
        schema.pop("title", None)
        return schema

    @classmethod
    def _validate_args(cls, action: dict, arguments: dict) -> str | None:
        try:
            cls._make_action_model(action)(**arguments)
            return None
        except Exception as exc:
            return str(exc)

    def _make_description(self, action: dict) -> str:
        display = self.clean_title(action)
        desc = action.get("description", "")
        icon = action.get("icon", "")
        icon_prefix = f"{icon} " if icon and not icon.startswith("<") else ""
        suffix = f" — {desc}" if desc else ""
        ro = self.is_read_only(action)
        approval_note = " No approval needed." if ro else " Requires human approval."
        return f"{icon_prefix}{display}{suffix}{approval_note}"

    @staticmethod
    def _make_annotations(action: dict) -> mt.ToolAnnotations:
        if ActionRegistry.is_read_only(action):
            return mt.ToolAnnotations(readOnlyHint=True, destructiveHint=False)
        return mt.ToolAnnotations(readOnlyHint=False, destructiveHint=True)

    # ── Action execution ──────────────────────────────────────────────────────

    async def run_action(self, action_id: str, arguments: dict) -> str:
        """
        Execute an action via OliveTin StartActionAndWait.
        Checks canExec and rate-limit state from cached Action proto data before calling.
        LogEntry proto fields checked: output, exitCode, timedOut, blocked,
        datetimeRateLimitExpires, executionTrackingId.
        """
        action = await self.get(action_id)
        if not action:
            return (
                f"Unknown action '{action_id}'. "
                "Call list_olivetin_actions() to see what's available."
            )

        if not action.get("canExec", True):
            rate_exp = action.get("datetimeRateLimitExpires", "")
            if rate_exp:
                return f"Action '{action_id}' is rate-limited until {rate_exp}. Try again later."
            return f"Action '{action_id}' cannot be executed right now (canExec=false)."

        err = self._validate_args(action, arguments)
        if err:
            return f"Validation error: {err}"

        olivetin_args = [{"name": k, "value": str(v)} for k, v in arguments.items()]
        timeout = min(max(int(action.get("timeout", 60)), 5), 600)

        try:
            result = await self._client.call(
                "StartActionAndWait",
                {"actionId": action_id, "arguments": olivetin_args},
                timeout=timeout + 10,
            )
        except httpx.HTTPStatusError as exc:
            log.error("olivetin_http_error", status=exc.response.status_code, action=action_id)
            return f"OliveTin API error (HTTP {exc.response.status_code}) for action '{action_id}'."
        except httpx.RequestError as exc:
            log.error("olivetin_unreachable", url=cfg.OLIVETIN_URL, error=str(exc))
            return (
                f"Could not reach OliveTin at {cfg.OLIVETIN_URL}. "
                "Check: Is OliveTin running? Is the URL correct? Is the network accessible?"
            )
        except Exception as exc:
            log.error("olivetin_error", action=action_id, error=str(exc))
            return f"OliveTin error executing action '{action_id}'. Check server logs for details."

        entry = result.get("logEntry") or {}

        if entry.get("blocked"):
            rate_exp = entry.get("datetimeRateLimitExpires", "")
            if rate_exp:
                return f"Blocked: action '{action_id}' is rate-limited until {rate_exp}."
            return f"Blocked: OliveTin rejected execution of '{action_id}'."

        if entry.get("timedOut"):
            return f"Error: action timed out after {timeout}s"

        output = (entry.get("output") or "").strip()
        exit_code = int(entry.get("exitCode") or 0)
        tracking_id = entry.get("executionTrackingId", "")
        log.info("action_completed", action=action_id, exit_code=exit_code, tracking_id=tracking_id)
        if exit_code != 0 and not output:
            return f"(exit code {exit_code})"
        return output or "(no output)"

    # ── Tool / prompt registration ────────────────────────────────────────────

    def _build_tool(self, action: dict) -> FunctionTool:
        action_id = action["bindingId"]
        registry = self  # capture for closure

        async def _handler(**kwargs: Any) -> str:
            log.info("tool_called", tool=action_id)
            async with registry.get_limiter(action_id):
                return await registry.run_action(action_id, kwargs)

        _handler.__name__ = f"_handler_{action_id}"
        return FunctionTool(
            name=action_id,
            description=self._make_description(action),
            parameters=self._make_input_schema(action),
            annotations=self._make_annotations(action),
            fn=_handler,
        )

    def _build_prompt(self, action: dict) -> str | None:
        """Register a per-action MCP prompt for actions that declare promptTemplate."""
        template = action.get("promptTemplate") or action.get("prompt_template") or ""
        if not template:
            return None
        action_id = action["bindingId"]
        display = self.clean_title(action)
        prompt_name = f"olivetin_{action_id}"
        _tmpl = template

        async def _prompt_fn(**kwargs: str) -> str:
            text = _tmpl
            for key, value in kwargs.items():
                text = text.replace(f"{{{key}}}", str(value))
            return text

        _prompt_fn.__name__ = prompt_name
        _prompt_fn.__doc__ = f"Prompt template for OliveTin action '{display}'."
        try:
            self._mcp.add_prompt(
                _prompt_fn, name=prompt_name, description=f"Prompt template for '{display}'."
            )
            return prompt_name
        except TypeError:
            try:
                self._mcp.add_prompt(_prompt_fn)
                return prompt_name
            except Exception as exc:
                log.warning("prompt_register_failed", prompt=prompt_name, error=str(exc))
        except Exception as exc:
            log.warning("prompt_register_failed", prompt=prompt_name, error=str(exc))
        return None

    async def _rebuild(self, new_actions: dict[str, dict]) -> None:
        for name in list(self._action_tool_names):
            try:
                self._mcp.local_provider.remove_tool(name)
            except Exception:
                pass
        self._action_tool_names.clear()

        for pname in list(self._prompt_names):
            try:
                self._mcp.remove_prompt(pname)
            except Exception:
                pass
        self._prompt_names.clear()

        new_readonly: set[str] = set()
        for action in new_actions.values():
            self._mcp.add_tool(self._build_tool(action))
            bid = action["bindingId"]
            self._action_tool_names.add(bid)
            if self.is_read_only(action):
                new_readonly.add(bid)
            pname = self._build_prompt(action)
            if pname:
                self._prompt_names.add(pname)

        self.readonly_tool_names = new_readonly | _STATIC_READONLY_TOOLS

    @staticmethod
    def _walk_dashboard(contents: list, acc: dict) -> None:
        """Recursively collect actions from GetDashboard → dashboard.contents.
        Skips hidden actions (hidden: true in OliveTin YAML)."""
        for item in contents:
            if item.get("action"):
                action = item["action"]
                bid = action.get("bindingId", "")
                if not bid:
                    continue
                if action.get("hidden"):
                    log.debug("skipping_hidden_action", binding_id=bid)
                    continue
                if bid in acc:
                    log.warning("duplicate_binding_id", binding_id=bid)
                    continue
                acc[bid] = action
            if item.get("contents"):
                ActionRegistry._walk_dashboard(item["contents"], acc)

    async def reload(self) -> None:
        try:
            result = await self._client.call("GetDashboard", {"title": "Home"})
            new_actions: dict[str, dict] = {}
            self._walk_dashboard(result.get("dashboard", {}).get("contents", []), new_actions)
            if not new_actions:
                result = await self._client.call("GetDashboard", {})
                self._walk_dashboard(result.get("dashboard", {}).get("contents", []), new_actions)

            async with self._lock:
                self._actions = new_actions

            await self._rebuild(new_actions)
            log.info("actions_loaded", count=len(new_actions), ids=sorted(new_actions.keys()))
            if not new_actions:
                log.warning("no_actions_loaded", olivetin=self._cfg.OLIVETIN_URL)
        except Exception as exc:
            log.warning("actions_load_failed", error=str(exc))

    async def refresh_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=float(self._cfg.ACTION_REFRESH_INTERVAL_SECS),
                )
                break
            except TimeoutError:
                await self.reload()


# ─── ApprovalManager ──────────────────────────────────────────────────────────
# Owns the pending approval queue and both approval paths:
#   1. MCP native elicitation (ctx.elicit) — inline dialog, 30s timeout
#   2. URL-based pending — human visits /pending/<token>, blocks until resolved
#
# The queue is exposed via:
#   GET  /pending                  — JSON list
#   GET  /pending/<token>          — HTML approval UI
#   POST /pending/<token>/approve  — approve
#   POST /pending/<token>/deny     — deny
#   MCP resource  olivetin://pending
#   MCP tool      list_pending_approvals


class ApprovalManager:
    def __init__(self, settings: Settings) -> None:
        self._cfg = settings
        self._pending: dict[str, dict] = {}

    # ── Elicitation (stage 1) ─────────────────────────────────────────────────

    async def try_elicitation(self, tool_name: str, args: dict, ctx: Any) -> bool | None:
        """
        Attempt MCP native elicitation. Returns True/False if the user responds,
        None if the client doesn't support elicitation or times out.
        """
        args_block = json.dumps(args, indent=2) if args else "none"
        message = (
            f"**Approve action?**\n\n"
            f"**Tool:** `{tool_name}`\n"
            f"**Arguments:**\n```json\n{args_block}\n```"
        )
        try:
            result = await asyncio.wait_for(
                ctx.elicit(message=message, response_type=bool),
                timeout=self._cfg.ELICITATION_TIMEOUT_SECS,
            )
            if isinstance(result, AcceptedElicitation):
                log.info("elicitation_accepted", tool=tool_name)
                return bool(result.data)
            if isinstance(result, (DeclinedElicitation, CancelledElicitation)):
                log.info("elicitation_declined", tool=tool_name)
                return False
            return False
        except TimeoutError:
            log.info("elicitation_timeout", tool=tool_name)
            return None
        except Exception:
            return None  # client doesn't support elicitation

    # ── URL-based pending (stage 2) ───────────────────────────────────────────

    async def pending_url_approval(self, tool_name: str, args: dict, display_name: str) -> bool:
        """
        Create a pending approval entry and block until a human visits
        APPROVAL_BASE_URL/pending/<token> and clicks Approve or Deny.
        """
        token = secrets.token_urlsafe(16)
        base = self._cfg.APPROVAL_BASE_URL or f"http://localhost:{self._cfg.HOST_BRIDGE_PORT}"
        url = f"{base}/pending/{token}"
        fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        self._pending[token] = {
            "future": fut,
            "action_id": tool_name,
            "title": display_name,
            "args": args,
            "created_at": time.time(),
            "expires_at": time.time() + self._cfg.APPROVAL_TIMEOUT_SECS,
        }
        log.info("approval_pending", tool=tool_name, url=url)
        try:
            return await asyncio.wait_for(fut, timeout=float(self._cfg.APPROVAL_TIMEOUT_SECS))
        except TimeoutError:
            log.warning("approval_timeout", tool=tool_name)
            return False
        finally:
            self._pending.pop(token, None)

    # ── Queue accessors (used by HTTP routes + MCP tools/resources) ───────────

    def get(self, token: str) -> dict | None:
        return self._pending.get(token)

    def approve(self, token: str) -> bool:
        p = self._pending.get(token)
        if p and not p["future"].done():
            p["future"].set_result(True)
            return True
        return False

    def deny(self, token: str) -> bool:
        p = self._pending.get(token)
        if p and not p["future"].done():
            p["future"].set_result(False)
            return True
        return False

    def list_pending(self) -> list[dict]:
        now = time.time()
        base = self._cfg.APPROVAL_BASE_URL or f"http://localhost:{self._cfg.HOST_BRIDGE_PORT}"
        return [
            {
                "token": token,
                "action_id": p["action_id"],
                "title": p["title"],
                "args": p["args"],
                "approval_url": f"{base}/pending/{token}",
                "expires_in": max(0, int(p["expires_at"] - now)),
            }
            for token, p in self._pending.items()
        ]


# ─── FastMCP app + lifespan ───────────────────────────────────────────────────

_client = OliveTinClient(cfg)
_approvals = ApprovalManager(cfg)


@asynccontextmanager
async def _lifespan(_server: Any):
    await _client.start()
    try:
        base = cfg.APPROVAL_BASE_URL or f"http://localhost:{cfg.HOST_BRIDGE_PORT}"
        log.info(
            "starting",
            olivetin=cfg.OLIVETIN_URL,
            olivetin_auth=_client.active_auth_modes(),
            approvals=f"{base}/pending/<token>",
            brave_search="configured" if BRAVE_API_KEY else "not configured",
            sse=f"http://0.0.0.0:{cfg.HOST_BRIDGE_PORT}/sse",
        )
        await _registry.reload()
        stop_event = asyncio.Event()
        refresh_task = asyncio.create_task(_registry.refresh_loop(stop_event))
        try:
            yield
        finally:
            stop_event.set()
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
    finally:
        await _client.close()


mcp = FastMCP("claw-host-bridge", lifespan=_lifespan)

# Registry is created after mcp so it can call mcp.add_tool / mcp.add_prompt.
# _lifespan captures it by name at call time, not at definition time — fine.
_registry = ActionRegistry(mcp, cfg, _client)


# ─── Pluggable approval fallback ──────────────────────────────────────────────
# Set this to an async callable to replace URL-based pending with a custom backend.
# Signature: async (tool_name: str, args: dict, display: str) -> bool
# Example: olivetin_mcp.approval_fallback = my_whatsapp_handler
approval_fallback: Any = None

# ─── Approval middleware ───────────────────────────────────────────────────────
# Intercepts every destructive tool call:
#   1. Tries MCP elicitation (inline dialog in Claude Desktop / Code).
#   2. Falls back to URL-based pending if elicitation is unsupported or times out.


class _ApprovalMiddleware(Middleware):
    async def on_call_tool(self, context: MiddlewareContext, call_next: Any) -> Any:
        tool_name: str = context.message.name

        if tool_name in _registry.readonly_tool_names:
            return await call_next(context)

        args = dict(context.message.arguments or {})
        fastmcp_ctx = getattr(context, "fastmcp_context", None)

        approved: bool | None = None
        if fastmcp_ctx is not None:
            approved = await _approvals.try_elicitation(tool_name, args, fastmcp_ctx)

        if approved is None:
            display = _registry.display_name(tool_name)
            if approval_fallback is not None:
                approved = await approval_fallback(tool_name, args, display)
            else:
                approved = await _approvals.pending_url_approval(tool_name, args, display)

        if not approved:
            log.info("tool_denied", tool=tool_name)
            return [mt.TextContent(type="text", text=f"⛔ Action '{tool_name}' was not approved.")]

        log.info("tool_approved", tool=tool_name)
        return await call_next(context)


mcp.add_middleware(_ApprovalMiddleware())


# ─── Pending approval HTTP routes ─────────────────────────────────────────────

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _html_response(content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(content, status_code=status_code, headers=_SECURITY_HEADERS)


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz_route(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/pending", methods=["GET"], include_in_schema=False)
async def pending_list_route(_request: Request) -> JSONResponse:
    return JSONResponse({"pending": _approvals.list_pending()})


@mcp.custom_route("/pending/{token}", methods=["GET"], include_in_schema=False)
async def pending_detail_route(request: Request) -> HTMLResponse:
    token = request.path_params.get("token", "")
    p = _approvals.get(token)
    if not p:
        return _html_response(
            _result_tmpl.render(
                icon="❓", title="Not Found", message="Already resolved or expired."
            ),
            status_code=404,
        )
    return _html_response(
        _approval_tmpl.render(
            title=p["title"],
            action_id=p["action_id"],
            args=p.get("args", {}),
            token=token,
            remaining=max(0, int(p["expires_at"] - time.time())),
        )
    )


@mcp.custom_route("/pending/{token}/approve", methods=["POST"], include_in_schema=False)
async def pending_approve_route(request: Request) -> HTMLResponse:
    token = request.path_params.get("token", "")
    p = _approvals.get(token)
    if not p:
        return _html_response(
            _result_tmpl.render(icon="⏰", title="Expired", message="Already resolved or expired."),
            status_code=410,
        )
    _approvals.approve(token)
    log.info("approved", tool=p["action_id"], token=token[:8])
    return _html_response(
        _result_tmpl.render(
            icon="✅",
            title="Approved",
            message=f"Action '{p['title']}' approved. The agent will proceed.",
        )
    )


@mcp.custom_route("/pending/{token}/deny", methods=["POST"], include_in_schema=False)
async def pending_deny_route(request: Request) -> HTMLResponse:
    token = request.path_params.get("token", "")
    p = _approvals.get(token)
    if not p:
        return _html_response(
            _result_tmpl.render(icon="⏰", title="Expired", message="Already resolved or expired."),
            status_code=410,
        )
    _approvals.deny(token)
    log.info("denied", tool=p["action_id"], token=token[:8])
    return _html_response(
        _result_tmpl.render(
            icon="❌",
            title="Denied",
            message=f"Action '{p['title']}' denied.",
        )
    )


# ─── Static MCP tools ─────────────────────────────────────────────────────────


@mcp.tool(
    name="list_pending_approvals",
    description=(
        "List all OliveTin actions currently waiting for human approval, "
        "with the URL the human must visit to approve or deny. "
        "Call this after triggering a destructive action to get the approval link. "
        "No approval needed — read-only."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def list_pending_approvals() -> str:
    items = _approvals.list_pending()
    if not items:
        return "No actions are currently pending approval."
    lines = [
        f"• **{p['title']}** (`{p['action_id']}`)\n"
        f"  Approval URL: {p['approval_url']}\n"
        f"  Args: {json.dumps(p['args'])}\n"
        f"  Expires in: {p['expires_in']}s"
        for p in items
    ]
    return "\n\n".join(lines)


@mcp.tool(
    name="brave_search",
    description=(
        "Search the web using the Brave Search API. "
        "Returns top results with title, URL, and description. "
        "No approval needed — read-only."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def brave_search(query: str, count: int = 10) -> str:
    if not BRAVE_API_KEY:
        return "Brave Search not configured (missing brave_api_key secret)."
    assert _client._http is not None
    try:
        resp = await _client._http.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(int(count), 20), "safesearch": "moderate"},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "Accept-Language": "en-US,en;q=0.9",
                "X-Subscription-Token": BRAVE_API_KEY,
            },
        )
        resp.raise_for_status()
        results = resp.json().get("web", {}).get("results", [])
        if not results:
            return "No results found."
        return "\n\n".join(
            f"**{r.get('title', '')}**\n{r.get('url', '')}\n{r.get('description', '')}"
            for r in results
        )
    except Exception as exc:
        return f"Brave Search error: {exc}"


async def _cleanup(path: str, delay: int = 60) -> None:
    await asyncio.sleep(delay)
    try:
        os.unlink(path)
    except OSError:
        pass


@mcp.tool(
    name="render_diagram",
    description=(
        "Render a Mermaid diagram spec as PNG via mermaid.ink. "
        "Returns /tmp path. No approval needed."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def render_diagram(spec: str) -> str:
    spec = spec.strip()
    if not spec:
        return "Error: spec is empty."
    if re.search(
        r"(file://|javascript:|data:|gopher://|ftp://|dict://|ldap://|tftp://|ssh://)",
        spec,
        re.I,
    ):
        return "Error: diagram spec contains disallowed content."
    assert _client._http is not None
    encoded = base64.urlsafe_b64encode(spec.encode()).decode().rstrip("=")
    try:
        resp = await _client._http.get(
            f"https://mermaid.ink/img/{encoded}?type=png",
            headers={"User-Agent": "claw-host-bridge/2.0"},
        )
        resp.raise_for_status()
        fd, out = tempfile.mkstemp(suffix=".png", prefix="diagram_")
        with os.fdopen(fd, "wb") as f:
            f.write(resp.content)
        asyncio.create_task(_cleanup(out))
        return out
    except Exception as exc:
        return f"render_diagram error: {exc}"


@mcp.tool(
    name="render_chart",
    description=(
        "Render a Chart.js config as PNG via quickchart.io. Returns /tmp path. No approval needed."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def render_chart(
    config: dict, width: int = 500, height: int = 300, background_color: str = "white"
) -> str:
    if not config:
        return "Error: config is empty."
    assert _client._http is not None
    try:
        resp = await _client._http.post(
            "https://quickchart.io/chart",
            json={
                "chart": config,
                "width": int(width),
                "height": int(height),
                "backgroundColor": background_color,
                "format": "png",
            },
        )
        resp.raise_for_status()
        fd, out = tempfile.mkstemp(suffix=".png", prefix="chart_")
        with os.fdopen(fd, "wb") as f:
            f.write(resp.content)
        asyncio.create_task(_cleanup(out))
        return out
    except Exception as exc:
        return f"render_chart error: {exc}"


@mcp.tool(
    name="list_olivetin_actions",
    description=(
        "List all available OliveTin actions: IDs, titles, argument schemas, "
        "and whether each requires approval. No approval needed."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def list_olivetin_actions() -> str:
    catalog = [
        {
            "id": a["bindingId"],
            "title": a.get("title", ""),
            "read_only": _registry.is_read_only(a),
            "approval_required": not _registry.is_read_only(a),
            "can_exec": a.get("canExec", True),
            "rate_limit_expires": a.get("datetimeRateLimitExpires", ""),
            "popup_on_start": a.get("popupOnStart", False),
            "order": a.get("order", 0),
            "args": [
                {
                    "name": arg["name"],
                    "type": arg.get("type", "string"),
                    "default": arg.get("defaultValue", "") or arg.get("default", ""),
                    "choices": [
                        {"value": c["value"], "title": c.get("title", c["value"])}
                        if isinstance(c, dict)
                        else {"value": c, "title": c}
                        for c in arg.get("choices", [])
                    ],
                }
                for arg in a.get("arguments", [])
                if arg.get("type") not in ("html", "confirmation")
            ],
        }
        for a in await _registry.snapshot()
    ]
    return json.dumps(catalog, indent=2)


@mcp.tool(
    name="get_execution_logs",
    description=(
        "Get recent OliveTin execution history (LogEntry records). "
        "Proto fields: datetime_started, action_title, output, exit_code, "
        "timed_out, user, execution_tracking_id, blocked. No approval needed."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def get_execution_logs(action_id: str = "", page_size: int = 50) -> str:
    try:
        if action_id:
            result = await _client.call("GetActionLogs", {"actionId": action_id})
        else:
            result = await _client.call("GetLogs", {"pageSize": page_size})
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"GetLogs error: {exc}"


@mcp.tool(
    name="get_execution_status",
    description=(
        "Check the status of a running OliveTin action by its execution_tracking_id "
        "(returned by StartAction). No approval needed."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def get_execution_status(execution_tracking_id: str) -> str:
    try:
        result = await _client.call(
            "ExecutionStatus",
            {"executionTrackingId": execution_tracking_id},
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"ExecutionStatus error: {exc}"


@mcp.tool(
    name="kill_action",
    description=(
        "Kill a currently running OliveTin action by its execution_tracking_id. "
        "Only works when the LogEntry's can_kill field is true. "
        "Returns killed/already_completed/not_found status. "
        "No approval needed — this is a safety stop, not a destructive write."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def kill_action(execution_tracking_id: str) -> str:
    try:
        result = await _client.call("KillAction", {"executionTrackingId": execution_tracking_id})
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"KillAction error: {exc}"


@mcp.tool(
    name="start_action_async",
    description=(
        "Start an OliveTin action asynchronously (StartAction RPC). "
        "Returns an execution_tracking_id immediately without waiting for completion. "
        "Use get_execution_status to poll progress, kill_action to cancel. "
        "Requires human approval — this triggers a real action on the host."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=False, destructiveHint=True),
)
async def start_action_async(action_id: str, arguments: dict | None = None) -> str:
    if arguments is None:
        arguments = {}
    action = await _registry.get(action_id)
    if not action:
        return f"Unknown action '{action_id}'. Call list_olivetin_actions() first."
    if not action.get("canExec", True):
        rate_exp = action.get("datetimeRateLimitExpires", "")
        if rate_exp:
            return f"Action '{action_id}' is rate-limited until {rate_exp}."
        return f"Action '{action_id}' cannot be executed right now (canExec=false)."
    olivetin_args = [{"name": k, "value": str(v)} for k, v in arguments.items()]
    tracking_id = secrets.token_urlsafe(16)
    try:
        result = await _client.call(
            "StartAction",
            {"bindingId": action_id, "arguments": olivetin_args, "uniqueTrackingId": tracking_id},
        )
        return json.dumps(result, indent=2)
    except httpx.HTTPStatusError as exc:
        log.error("start_action_http_error", status=exc.response.status_code, action=action_id)
        return f"OliveTin API error (HTTP {exc.response.status_code}) for action '{action_id}'."
    except Exception as exc:
        log.error("start_action_error", action=action_id, error=str(exc))
        return f"StartAction error for '{action_id}'. Check server logs for details."


@mcp.tool(
    name="restart_action",
    description=(
        "Restart a previously executed OliveTin action by its execution_tracking_id "
        "(RestartAction RPC). Re-runs with the same arguments as the original execution. "
        "Requires human approval — this re-triggers a real action on the host."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=False, destructiveHint=True),
)
async def restart_action(execution_tracking_id: str) -> str:
    try:
        result = await _client.call("RestartAction", {"executionTrackingId": execution_tracking_id})
        return json.dumps(result, indent=2)
    except httpx.HTTPStatusError as exc:
        log.error("restart_action_http_error", status=exc.response.status_code)
        return f"OliveTin API error (HTTP {exc.response.status_code}) for RestartAction."
    except Exception as exc:
        log.error("restart_action_error", error=str(exc))
        return "RestartAction error. Check server logs for details."


@mcp.tool(
    name="validate_argument",
    description=(
        "Validate an argument value against an OliveTin argument type (ValidateArgumentType RPC). "
        "type: unicode | ascii_identifier | url | int | float | bool. "
        "No approval needed — read-only."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def validate_argument(
    value: str, type: str, binding_id: str = "", argument_name: str = ""
) -> str:
    try:
        payload: dict = {"value": value, "type": type}
        if binding_id:
            payload["bindingId"] = binding_id
        if argument_name:
            payload["argumentName"] = argument_name
        result = await _client.call("ValidateArgumentType", payload)
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"ValidateArgumentType error: {exc}"


@mcp.tool(
    name="whoami",
    description=(
        "Return information about the current authenticated OliveTin user (WhoAmI RPC). "
        "No approval needed — read-only."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def whoami() -> str:
    try:
        result = await _client.call("WhoAmI", {})
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"WhoAmI error: {exc}"


@mcp.tool(
    name="get_server_info",
    description=(
        "Return OliveTin server info and version (Init RPC). No approval needed — read-only."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def get_server_info() -> str:
    try:
        result = await _client.call("Init", {})
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"Init error: {exc}"


@mcp.tool(
    name="get_action_binding",
    description=(
        "Fetch a single OliveTin action binding by ID (GetActionBinding RPC). "
        "Returns full Action proto including arguments, icon, timeout, and ACL fields. "
        "No approval needed — read-only."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def get_action_binding(binding_id: str) -> str:
    try:
        result = await _client.call("GetActionBinding", {"bindingId": binding_id})
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"GetActionBinding error: {exc}"


@mcp.tool(
    name="get_entities",
    description=(
        "List all OliveTin entities (GetEntities RPC). "
        "Entities represent monitored services/hosts. "
        "No approval needed — read-only."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def get_entities() -> str:
    try:
        result = await _client.call("GetEntities", {})
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"GetEntities error: {exc}"


@mcp.tool(
    name="get_entity",
    description=(
        "Fetch a single OliveTin entity by unique key and type (GetEntity RPC). "
        "No approval needed — read-only."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def get_entity(unique_key: str, type: str = "") -> str:
    try:
        payload: dict = {"uniqueKey": unique_key}
        if type:
            payload["type"] = type
        result = await _client.call("GetEntity", payload)
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"GetEntity error: {exc}"


@mcp.tool(
    name="readyz",
    description=(
        "OliveTin readiness probe (GetReadyz RPC). "
        "Returns 200/ready when OliveTin is healthy. "
        "No approval needed — read-only."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def readyz() -> str:
    try:
        result = await _client.call("GetReadyz", {})
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"GetReadyz error: {exc}"


@mcp.tool(
    name="reload_actions",
    description=(
        "Force an immediate refresh of the OliveTin action catalog from GetDashboard. "
        "Use this after adding, removing, or editing actions in OliveTin config "
        "without waiting for the automatic refresh interval. "
        "No approval needed — read-only."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def reload_actions_tool() -> str:
    before = len(await _registry.snapshot())
    await _registry.reload()
    after = len(await _registry.snapshot())
    return f"Reloaded. {after} action(s) available (was {before})."


# ─── MCP Resources ────────────────────────────────────────────────────────────


@mcp.resource(
    "olivetin://actions",
    name="OliveTin Actions Catalog",
    description=(
        "Full catalog of OliveTin actions with argument schemas (from ActionArgument proto), "
        "read-only status, approval requirements, and prompt templates. "
        "Read this before calling any action."
    ),
    mime_type="application/json",
)
async def resource_olivetin_actions() -> str:
    return json.dumps(
        [
            {
                "id": a["bindingId"],
                "title": a.get("title", ""),
                "description": a.get("description") or "",
                "icon": a.get("icon", ""),
                "read_only": _registry.is_read_only(a),
                "approval_required": not _registry.is_read_only(a),
                "timeout_secs": a.get("timeout", 60),
                "can_exec": a.get("canExec", True),
                "rate_limit_expires": a.get("datetimeRateLimitExpires", ""),
                "popup_on_start": a.get("popupOnStart", False),
                "order": a.get("order", 0),
                "args": [
                    {
                        "name": arg["name"],
                        "type": arg.get("type", "string"),
                        "description": arg.get("description") or arg.get("title") or "",
                        "default": arg.get("defaultValue", "") or arg.get("default", ""),
                        "required": not bool(arg.get("defaultValue", "") or arg.get("default", "")),
                        "choices": [
                            {"value": c["value"], "title": c.get("title", c["value"])}
                            if isinstance(c, dict)
                            else {"value": c, "title": c}
                            for c in arg.get("choices", [])
                        ],
                        "suggestions": arg.get("suggestions") or {},
                    }
                    for arg in a.get("arguments", [])
                    if arg.get("type") not in ("html", "confirmation")
                ],
                "prompt_template": a.get("promptTemplate") or a.get("prompt_template") or "",
            }
            for a in await _registry.snapshot()
        ],
        indent=2,
    )


def _can_exec_label(can_exec: bool, rate_exp: str) -> str:
    if can_exec:
        return "Yes"
    if rate_exp:
        return f"No — rate-limited until {rate_exp}"
    return "No"


@mcp.resource(
    "olivetin://actions/{action_id}/help",
    name="OliveTin Action Help",
    description="Full documentation for a specific OliveTin action.",
    mime_type="text/plain",
)
async def resource_olivetin_action_help(action_id: str) -> str:
    action = await _registry.get(action_id)
    if not action:
        return f"Unknown action '{action_id}'.\nRead olivetin://actions to see the catalog."

    display = _registry.clean_title(action)
    read_only = _registry.is_read_only(action)
    base = cfg.APPROVAL_BASE_URL or f"http://localhost:{cfg.HOST_BRIDGE_PORT}"
    args = [a for a in action.get("arguments", []) if a.get("type") not in ("html", "confirmation")]
    can_exec = action.get("canExec", True)
    rate_exp = action.get("datetimeRateLimitExpires", "")

    lines = [
        f"# {display}",
        "",
        f"**ID**: `{action_id}`",
        f"**Approval**: {'No — read-only' if read_only else f'Yes — visit {base}/pending/<token>'}",
        f"**Timeout**: {action.get('timeout', 60)}s",
        f"**Can execute**: {_can_exec_label(can_exec, rate_exp)}",
        f"**Order**: {action.get('order', 0)}",
        f"**Popup on start**: {'Yes' if action.get('popupOnStart') else 'No'}",
    ]
    if action.get("description"):
        lines += ["", "## Description", "", action["description"]]
    lines += ["", "## Arguments", ""]
    if args:
        for arg in args:
            name = arg["name"]
            default = arg.get("defaultValue", "") or arg.get("default", "")
            desc = arg.get("description") or arg.get("title") or ""
            lines.append(f"### `{name}` ({arg.get('type', 'string')})")
            if desc:
                lines.append(desc)
            lines.append(f"- Required: {'Yes' if not default else f'No (default: {default!r})'}")
            choices = arg.get("choices", [])
            if choices:
                choice_strs = []
                for c in choices:
                    if isinstance(c, dict):
                        v, t = c.get("value", ""), c.get("title", "")
                        choice_strs.append(f"{t!r} ({v!r})" if t and t != v else repr(v))
                    else:
                        choice_strs.append(repr(c))
                lines.append(f"- Choices: {', '.join(choice_strs)}")
            lines.append("")
    else:
        lines += ["No arguments.", ""]

    tpl = action.get("promptTemplate") or action.get("prompt_template") or ""
    if tpl:
        lines += ["## Prompt Template", "", tpl, ""]
    return "\n".join(lines)


@mcp.resource(
    "olivetin://logs",
    name="OliveTin Execution Logs",
    description=(
        "Recent execution history across all actions (LogEntry proto records: "
        "datetime_started, output, exit_code, timed_out, user, execution_tracking_id, blocked)."
    ),
    mime_type="application/json",
)
async def resource_olivetin_logs() -> str:
    try:
        result = await _client.call("GetLogs", {})
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps({"error": f"GetLogs failed: {exc}"})


@mcp.resource(
    "olivetin://logs/{action_id}",
    name="OliveTin Action Logs",
    description="Execution history for a specific OliveTin action.",
    mime_type="application/json",
)
async def resource_olivetin_action_logs(action_id: str) -> str:
    if not await _registry.known(action_id):
        return json.dumps(
            {"error": f"Unknown action '{action_id}'. Read olivetin://actions first."}
        )
    try:
        result = await _client.call("GetActionLogs", {"actionId": action_id})
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps({"error": f"GetActionLogs failed: {exc}"})


@mcp.resource(
    "olivetin://pending",
    name="Pending Approvals",
    description=(
        "Live list of actions waiting for human approval, with approval URLs. "
        "Read this after triggering a destructive action to get the link to share."
    ),
    mime_type="application/json",
)
async def resource_pending_approvals() -> str:
    return json.dumps({"pending": _approvals.list_pending()}, indent=2)


# ─── MCP Prompts ──────────────────────────────────────────────────────────────


@mcp.prompt(
    name="olivetin_run",
    description=(
        "Compose a prompt for running an OliveTin action with formatted output. "
        "output_format: plain | json | markdown | summary"
    ),
)
async def prompt_olivetin_run(action_id: str, output_format: str = "plain") -> str:
    action = await _registry.get(action_id)
    if not action:
        return (
            f"Action '{action_id}' not found. "
            "Read olivetin://actions to discover available actions."
        )

    display = _registry.clean_title(action)
    args_desc = ", ".join(
        f"`{a['name']}` ({a.get('type', 'string')})"
        for a in action.get("arguments", [])
        if a.get("type") not in ("html", "confirmation")
    )
    format_instructions = {
        "json": (
            "Return the result as a JSON object with `output`, `exit_code`, and `action_id` keys."
        ),
        "markdown": (
            "Format the output in a Markdown code block with appropriate syntax highlighting."
        ),
        "summary": ("Summarise the result in 1–3 sentences, calling out any warnings or errors."),
    }.get(output_format, "Return the raw output text as-is.")

    tpl = action.get("promptTemplate") or action.get("prompt_template") or ""
    if tpl:
        return f"{tpl}\n\n{format_instructions}"
    base_text = f"Run the OliveTin action `{action_id}` ({display})."
    if args_desc:
        base_text += f" Required arguments: {args_desc}."
    return f"{base_text}\n\n{format_instructions}"


# ─── Entry point ──────────────────────────────────────────────────────────────


if __name__ == "__main__":  # pragma: no cover
    mcp.run(
        transport="sse",
        host="0.0.0.0",
        port=cfg.HOST_BRIDGE_PORT,
        path="/sse",
        log_level="warning",
        show_banner=False,
    )
