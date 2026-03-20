#!/usr/bin/env python3
"""
claw-host-bridge — OliveTin MCP wrapper with approval middleware
──────────────────────────────────────────────────────────────────
Runs inside Docker and exposes OliveTin actions plus static helper tools via
FastMCP over SSE.

Approval flow for write actions:
  1) MCP-native elicitation dialog (Claude Code/Desktop)
  2) if user ignores timeout, fall through to WhatsApp poll via nanoclaw
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import os
import secrets
import re
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager

import httpx
import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from mcp_approval_proxy import ApprovalMiddleware
from mcp_approval_proxy.engines import WhatsAppEngine
from mcp_approval_proxy.transports import TransportPolicy
from starlette.requests import Request
from starlette.responses import JSONResponse


# ─── Config ───────────────────────────────────────────────────────────────────

PORT = int(os.environ.get("HOST_BRIDGE_PORT", "9003"))
NANOCLAW_APPROVALS_URL = os.environ.get("NANOCLAW_APPROVALS_URL", "http://nanoclaw:3002")
APPROVAL_TIMEOUT = int(os.environ.get("APPROVAL_TIMEOUT_SECS", "120"))
OLIVETIN_URL = os.environ.get("OLIVETIN_URL", "http://host.docker.internal:1337")
_SECRETS_DIR = os.environ.get("SECRETS_DIR", "/run/secrets")
WHATSAPP_POLL_CMD = os.environ.get("WHATSAPP_POLL_CMD", "").strip()
# Deprecated: WAHA is no longer in docker-compose. Set WAHA_URL to re-enable.
WAHA_URL = os.environ.get("WAHA_URL", "").rstrip("/")
WAHA_SESSION = os.environ.get("WAHA_SESSION", "default").strip() or "default"
WAHA_CHAT_ID = os.environ.get("WAHA_CHAT_ID", "").strip()
WAHA_WEBHOOK_PATH = os.environ.get("WAHA_WEBHOOK_PATH", "/waha/webhook").strip() or "/waha/webhook"
WAHA_API_KEY = os.environ.get("WAHA_API_KEY", "").strip()
_SELF_BRIDGE_URL = f"http://127.0.0.1:{PORT}"

# Approval gate is enabled only when URL is configured.
# Empty URL blocks write actions rather than auto-approving.
_APPROVAL_GATE_ENABLED = bool(NANOCLAW_APPROVALS_URL)

# Rate limiting
_last_call: dict[str, float] = {}
_MIN_INTERVAL = 0.5
_waha_pending_votes: dict[str, dict] = {}
_waha_pending_tokens: dict[str, str] = {}


# ─── FastMCP setup ────────────────────────────────────────────────────────────

@asynccontextmanager
async def _hostbridge_lifespan(_server):
    print(f"[host-bridge] Starting — OliveTin at {OLIVETIN_URL}", flush=True)
    if WHATSAPP_POLL_CMD:
        print(f"[host-bridge] Approvals via CLI: {WHATSAPP_POLL_CMD}", flush=True)
    elif WAHA_URL:
        print(f"[host-bridge] Approvals via WAHA: {WAHA_URL} session={WAHA_SESSION} chat={WAHA_CHAT_ID or '(missing)'}", flush=True)
    else:
        print(f"[host-bridge] Approvals via nanoclaw: {NANOCLAW_APPROVALS_URL or '(disabled — writes BLOCKED)'}", flush=True)
    print(f"[host-bridge] Brave Search: {'✓ configured' if BRAVE_API_KEY else '✗ not configured'}", flush=True)
    print(f"[host-bridge] SSE: http://0.0.0.0:{PORT}/sse", flush=True)

    await _reload_actions()

    stop_event = asyncio.Event()
    refresh_task = asyncio.create_task(_refresh_loop(stop_event))
    try:
        yield
    finally:
        stop_event.set()
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass


mcp = FastMCP("claw-host-bridge", lifespan=_hostbridge_lifespan)


# ─── Secret helpers ───────────────────────────────────────────────────────────

def _read_secret(name: str) -> str:
    for candidate in [name, name.split(".")[0]]:
        path = os.path.join(_SECRETS_DIR, candidate)
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
    return ""


BRAVE_API_KEY = _read_secret("brave_api_key")


# ─── OliveTin client ──────────────────────────────────────────────────────────

def _olivetin_call(endpoint: str, payload: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{OLIVETIN_URL}/api/{endpoint}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


async def _olivetin(endpoint: str, payload: dict, timeout: int = 30) -> dict:
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(None, _olivetin_call, endpoint, payload, timeout),
        timeout=float(timeout + 5),
    )


# ─── Action registry (dynamic tools) ──────────────────────────────────────────

_actions: dict[str, dict] = {}
_actions_lock = asyncio.Lock()
_action_tool_names: set[str] = set()
_prompt_names: set[str] = set()


def _walk_dashboard(contents: list, new_actions: dict) -> None:
    for item in contents:
        if item.get("action"):
            action = item["action"]
            binding_id = action["bindingId"]
            if binding_id in new_actions:
                print(f"[host-bridge] WARNING: duplicate bindingId '{binding_id}' — skipping", flush=True)
                continue
            new_actions[binding_id] = action
        if item.get("contents"):
            _walk_dashboard(item["contents"], new_actions)


def _is_read_only(action: dict) -> bool:
    title = action.get("title", "").lower()
    binding_id = action.get("bindingId", "").lower()
    return title.startswith("[read-only]") or binding_id.startswith("ro_")


def _validate_args(action: dict, arguments: dict) -> str | None:
    for arg in action.get("arguments", []):
        name = arg["name"]
        if name not in arguments:
            continue

        value = arguments[name]
        arg_type = arg.get("type", "unicode")
        choices = [c["value"] if isinstance(c, dict) else c for c in arg.get("choices", [])]

        if choices and str(value) not in choices:
            return f"Argument '{name}' must be one of: {choices}. Got: {value!r}"
        if arg_type == "int":
            try:
                int(value)
            except (ValueError, TypeError):
                return f"Argument '{name}' must be an integer. Got: {value!r}"
        if arg_type == "float":
            try:
                float(value)
            except (ValueError, TypeError):
                return f"Argument '{name}' must be a number. Got: {value!r}"
    return None


def _make_input_schema(action: dict) -> dict:
    properties: dict[str, dict] = {}
    required: list[str] = []

    for arg in action.get("arguments", []):
        name = arg["name"]
        arg_type = arg.get("type", "unicode")
        raw_choices = arg.get("choices", [])
        choices = [c["value"] if isinstance(c, dict) else c for c in raw_choices]
        default = arg.get("defaultValue", "")

        if choices:
            prop: dict = {"type": "string", "enum": choices}
        elif arg_type == "int":
            prop = {"type": "integer"}
        elif arg_type == "float":
            prop = {"type": "number"}
        elif arg_type == "bool":
            prop = {"type": "boolean"}
        else:
            prop = {"type": "string"}

        description = arg.get("description") or arg.get("title") or ""
        if description:
            prop["description"] = description
        if default:
            prop["default"] = default

        properties[name] = prop
        if not default:
            required.append(name)

    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _make_action_description(action: dict) -> str:
    title = action.get("title", action["bindingId"])
    display = title.replace("[read-only]", "").replace("[READ-ONLY]", "").strip()
    approval_note = " No approval needed." if _is_read_only(action) else " Requires approval."
    return display + approval_note


def _make_action_annotations(action: dict) -> mt.ToolAnnotations:
    if _is_read_only(action):
        return mt.ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    return mt.ToolAnnotations(readOnlyHint=False, destructiveHint=True)


async def _run_olivetin_action(action_id: str, arguments: dict) -> str:
    async with _actions_lock:
        action = _actions.get(action_id)
    if not action:
        return f"Unknown action '{action_id}'. Call list_olivetin_actions() to see what's available."

    validation_error = _validate_args(action, arguments)
    if validation_error:
        return f"Validation error: {validation_error}"

    try:
        olivetin_args = [{"name": key, "value": str(value)} for key, value in arguments.items()]
        timeout = min(max(int(action.get("timeout", 60)), 5), 600)
        result = await _olivetin(
            "StartActionAndWait",
            {"actionId": action_id, "arguments": olivetin_args},
            timeout=timeout + 10,
        )
    except urllib.error.URLError:
        return "Error: could not reach execution backend."
    except Exception as exc:
        return f"OliveTin error: {exc}"

    entry = result.get("logEntry") or {}
    if entry.get("timedOut"):
        return f"Error: timed out after {timeout}s"
    output = (entry.get("output") or "").strip()
    exit_code = int(entry.get("exitCode") or 0)
    if exit_code != 0 and not output:
        return f"(exit code {exit_code})"
    return output or "(no output)"


def _build_action_tool(action: dict) -> FunctionTool:
    action_id = action["bindingId"]

    async def _handler(**kwargs):
        call_id = f"{action_id}-{int(time.time() * 1000)}"
        print(f"[host-bridge] [{call_id}] call tool={action_id}", flush=True)

        now = time.time()
        if now - _last_call.get(action_id, 0) < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - (now - _last_call.get(action_id, 0)))
        _last_call[action_id] = time.time()

        return await _run_olivetin_action(action_id, kwargs)

    return FunctionTool(
        name=action_id,
        description=_make_action_description(action),
        parameters=_make_input_schema(action),
        annotations=_make_action_annotations(action),
        fn=_handler,
    )


async def _rebuild_action_tools(new_actions: dict[str, dict]) -> None:
    # Replace all dynamic tools in one pass so the exposed catalog always matches
    # the latest OliveTin dashboard.
    for tool_name in list(_action_tool_names):
        try:
            mcp.remove_tool(tool_name)
        except Exception:
            pass
    _action_tool_names.clear()

    # Remove dynamic per-action prompts (for actions with promptTemplate).
    for prompt_name in list(_prompt_names):
        try:
            mcp.remove_prompt(prompt_name)
        except Exception:
            pass
    _prompt_names.clear()

    for action in new_actions.values():
        tool = _build_action_tool(action)
        mcp.add_tool(tool)
        _action_tool_names.add(action["bindingId"])

        prompt_name = _build_action_prompt(action)
        if prompt_name:
            _prompt_names.add(prompt_name)


# ─── WhatsApp approval backend (for middleware fallback) ─────────────────────

async def _proxy_whatsapp_poll(question: str, options: list[str], timeout_secs: int) -> str | None:
    """
    Proxies approval request to nanoclaw /approvals API (WhatsApp poll via Baileys).
    Optional overrides: WHATSAPP_POLL_CMD (CLI) or WAHA_URL (deprecated).
    """
    if len(options) < 2:
        options = ["✅ Approve", "❌ Deny"]

    timeout_secs = max(1, int(timeout_secs))

    # Optional CLI override.
    if WHATSAPP_POLL_CMD:
        try:
            argv = shlex.split(WHATSAPP_POLL_CMD)
            if not argv:
                raise RuntimeError("WHATSAPP_POLL_CMD is empty after parsing")
            payload = json.dumps(
                {
                    "question": question,
                    "options": options,
                    "timeoutMs": timeout_secs * 1000,
                },
                ensure_ascii=False,
            ).encode()
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout=timeout_secs + 20)
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")[:500]
                print(f"[host-bridge] approval CLI failed rc={proc.returncode}: {err}", flush=True)
                return options[1]
            data = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
            choice = str(data.get("choice", "")).strip()
            if choice not in options:
                print(f"[host-bridge] approval CLI returned invalid choice={choice!r}", flush=True)
                return options[1]
            print(f"[host-bridge] approval CLI choice={choice!r}", flush=True)
            return choice
        except TimeoutError:
            print("[host-bridge] approval CLI timed out", flush=True)
            return options[1]
        except Exception as exc:
            print(f"[host-bridge] approval CLI error: {exc}", flush=True)
            return options[1]

    # Preferred hosted backend: WAHA with webhook vote callbacks (deprecated — not in compose).
    if WAHA_URL:
        if not WAHA_CHAT_ID:
            print("[host-bridge] WAHA_URL is set but WAHA_CHAT_ID is missing", flush=True)
            return options[1]
        return await _waha_send_poll_and_wait(question, options, timeout_secs)

    # Default: nanoclaw approvals API — sends WhatsApp poll via Baileys.
    if not _APPROVAL_GATE_ENABLED:
        print("[host-bridge] WARNING: Approval backend unavailable — /whatsapp_poll denied", flush=True)
        return None

    try:
        payload = json.dumps({"message": question, "timeoutMs": timeout_secs * 1000}).encode()
        req = urllib.request.Request(
            f"{NANOCLAW_APPROVALS_URL}/approvals",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            approval_id = json.loads(resp.read())["id"]
    except Exception as exc:
        print(f"[host-bridge] approval POST failed: {exc}", flush=True)
        return None

    print(f"[host-bridge] approval poll sent: {approval_id} — {question[:120]}", flush=True)
    deadline = time.time() + timeout_secs + 15

    while time.time() < deadline:
        await asyncio.sleep(1)
        try:
            get_req = urllib.request.Request(
                f"{NANOCLAW_APPROVALS_URL}/approvals/{approval_id}",
                method="GET",
            )
            with urllib.request.urlopen(get_req, timeout=5) as resp:
                status = json.loads(resp.read()).get("status", "pending")
            if status == "pending":
                continue
            approved = status == "approved"
            choice = options[0] if approved else options[1]
            print(
                f"[host-bridge] approval {'✓ granted' if approved else '✗ denied'}: {approval_id} choice={choice!r}",
                flush=True,
            )
            return choice
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                print(f"[host-bridge] approval {approval_id} expired/not found", flush=True)
                return options[1]
            await asyncio.sleep(2)
        except Exception:
            await asyncio.sleep(2)

    print(f"[host-bridge] approval timed out: {approval_id}", flush=True)
    return options[1]


async def _waha_send_poll_and_wait(question: str, options: list[str], timeout_secs: int) -> str:
    """
    Send poll via WAHA and wait for a poll.vote webhook callback.
    """
    multiple_answers = False
    session = (WAHA_SESSION or os.environ.get("WAHA_SESSION", "default")).strip() or "default"
    chat_id = (WAHA_CHAT_ID or os.environ.get("WAHA_CHAT_ID", "")).strip()
    api_key = (WAHA_API_KEY or os.environ.get("WAHA_API_KEY", "")).strip()
    token = secrets.token_hex(2).upper()
    payload = {
        "session": session,
        "chatId": chat_id,
        "poll": {
            "name": question[:300],
            "options": options,
            "multipleAnswers": multiple_answers,
        },
    }

    poll_id = ""
    try:
        if not chat_id:
            raise RuntimeError("WAHA_CHAT_ID missing")

        print(
            f"[host-bridge] WAHA sendPoll request session={session} chat={chat_id} key_len={len(api_key)}",
            flush=True,
        )
        async with httpx.AsyncClient(timeout=15) as client:
            auth_headers = []
            if api_key:
                auth_headers = [
                    ("X-Api-Key", {"X-Api-Key": api_key}),
                    ("X-API-KEY", {"X-API-KEY": api_key}),
                    ("Authorization Bearer", {"Authorization": f"Bearer {api_key}"}),
                ]
            else:
                auth_headers = [("no auth", {})]

            last_error: Exception | None = None
            for label, headers in auth_headers:
                try:
                    resp = await client.post(f"{WAHA_URL}/api/sendPoll", json=payload, headers=headers or None)
                    resp.raise_for_status()
                    poll_id = str(resp.json().get("id", "")).strip()
                    if not poll_id:
                        raise RuntimeError("WAHA sendPoll response missing id")
                    print(f"[host-bridge] WAHA sendPoll auth ok: {label}", flush=True)
                    break
                except Exception as exc:  # try the next auth mode
                    last_error = exc
                    print(f"[host-bridge] WAHA sendPoll auth failed: {label}: {exc}", flush=True)
                    continue
            if not poll_id and last_error:
                raise last_error

            # Reliable fallback for engines where poll vote decrypt can fail.
            reply_help = (
                f"{question}\n\n"
                f"If poll tap doesn't register, reply:\n"
                f"APPROVE {token}\n"
                f"or\n"
                f"DENY {token}"
            )
            await client.post(
                f"{WAHA_URL}/api/sendText",
                json={"session": session, "chatId": chat_id, "text": reply_help[:1200]},
                headers={"X-Api-Key": api_key} if api_key else None,
            )
    except Exception as exc:
        print(f"[host-bridge] WAHA sendPoll failed: {exc}", flush=True)
        return options[1]

    loop = asyncio.get_event_loop()
    fut: asyncio.Future[str] = loop.create_future()
    _waha_pending_votes[poll_id] = {
        "future": fut,
        "options": list(options),
        "token": token,
        "expires_at": time.time() + timeout_secs + 20,
    }
    _waha_pending_tokens[token] = poll_id
    print(f"[host-bridge] WAHA poll sent id={poll_id}", flush=True)

    try:
        selected = await asyncio.wait_for(fut, timeout=timeout_secs + 20)
        if selected in options:
            return selected
        return options[1]
    except TimeoutError:
        print(f"[host-bridge] WAHA poll timed out id={poll_id}", flush=True)
        return options[1]
    finally:
        _waha_pending_tokens.pop(token, None)
        _waha_pending_votes.pop(poll_id, None)


@mcp.custom_route(WAHA_WEBHOOK_PATH, methods=["POST"], include_in_schema=False)
async def waha_webhook_route(request: Request):
    """
    WAHA webhook receiver for poll.vote events.
    Configure WAHA webhook URL to hit this route from your network.
    """
    try:
        event = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    try:
        event_name = str(event.get("event", ""))
        print(f"[host-bridge] WAHA webhook event={event_name}", flush=True)
        if event_name in {"message", "message.any"}:
            payload = event.get("payload", {}) if isinstance(event.get("payload"), dict) else {}
            from_me = bool(payload.get("fromMe"))
            if from_me:
                return JSONResponse({"ok": True}, status_code=200)

            body = str(payload.get("body", "")).strip()
            if not body:
                msg = payload.get("message", {}) if isinstance(payload.get("message"), dict) else {}
                body = str(
                    msg.get("conversation")
                    or msg.get("extendedTextMessage", {}).get("text")
                    or ""
                ).strip()
            if not body:
                return JSONResponse({"ok": True}, status_code=200)

            body_l = body.lower()
            decision = ""
            if "approve" in body_l:
                decision = "Approve"
            elif "deny" in body_l:
                decision = "Deny"
            if not decision:
                return JSONResponse({"ok": True}, status_code=200)

            matched_poll_id = ""
            for token, p_id in list(_waha_pending_tokens.items()):
                if token.lower() in body_l:
                    matched_poll_id = p_id
                    break
            if not matched_poll_id and len(_waha_pending_votes) == 1:
                matched_poll_id = next(iter(_waha_pending_votes.keys()))
            if not matched_poll_id:
                return JSONResponse({"ok": True}, status_code=200)

            pending = _waha_pending_votes.get(matched_poll_id)
            if not pending:
                return JSONResponse({"ok": True}, status_code=200)
            options = pending["options"]
            selected = options[0] if decision == "Approve" else (options[1] if len(options) > 1 else "")
            future = pending["future"]
            if selected and not future.done():
                future.set_result(selected)
                print(f"[host-bridge] WAHA text vote id={matched_poll_id} selected={selected!r}", flush=True)
            return JSONResponse({"ok": True}, status_code=200)

        if event_name not in {"poll.vote", "poll.vote.failed"}:
            return JSONResponse({"ok": True}, status_code=200)

        payload = event.get("payload", {}) if isinstance(event.get("payload"), dict) else {}
        poll = payload.get("poll", {}) if isinstance(payload.get("poll"), dict) else {}
        vote = payload.get("vote", {}) if isinstance(payload.get("vote"), dict) else {}
        if event_name == "poll.vote.failed":
            print(
                f"[host-bridge] WAHA poll.vote.failed payload={json.dumps(payload, ensure_ascii=True)[:1200]}",
                flush=True,
            )
        poll_id = str(poll.get("id", "")).strip()
        selected_options = vote.get("selectedOptions", [])
        if not poll_id:
            # Keep one-line debug so we can adapt to WAHA payload changes quickly.
            print(f"[host-bridge] WAHA poll payload keys={list(payload.keys())}", flush=True)
        if not poll_id or not isinstance(selected_options, list):
            return JSONResponse({"ok": True}, status_code=200)

        pending = _waha_pending_votes.get(poll_id)
        if not pending:
            return JSONResponse({"ok": True}, status_code=200)

        future = pending["future"]
        options = pending["options"]
        selected = ""
        for item in selected_options:
            candidate = str(item)
            if candidate in options:
                selected = candidate
                break
        if not selected:
            # For poll.vote.failed WAHA can send empty options on decrypt failure.
            # Keep waiting for text fallback instead of auto-denying immediately.
            if event_name == "poll.vote.failed":
                return JSONResponse({"ok": True}, status_code=200)
            selected = options[1] if len(options) > 1 else ""
        if not future.done():
            future.set_result(selected)
            print(f"[host-bridge] WAHA poll vote id={poll_id} selected={selected!r}", flush=True)
    except Exception as exc:
        print(f"[host-bridge] WAHA webhook parse error: {exc}", flush=True)

    return JSONResponse({"ok": True}, status_code=200)


@mcp.custom_route("/whatsapp_poll", methods=["POST"], include_in_schema=False)
async def whatsapp_poll_route(request: Request):
    """
    Legacy compatibility route expected by WhatsAppEngine(api_mode='whatsapp_poll').
    Body: {question: str, options: [approve, deny], timeoutMs?: int}
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    question = str(payload.get("question", "")).strip()
    if not question:
        return JSONResponse({"error": "question is required"}, status_code=400)

    raw_options = payload.get("options")
    options = [str(v) for v in raw_options] if isinstance(raw_options, list) else ["✅ Approve", "❌ Deny"]
    if len(options) < 2:
        options = ["✅ Approve", "❌ Deny"]

    timeout_ms = payload.get("timeoutMs")
    timeout_secs = APPROVAL_TIMEOUT
    if isinstance(timeout_ms, (int, float)) and timeout_ms > 0:
        timeout_secs = int(timeout_ms / 1000)

    choice = await _proxy_whatsapp_poll(question, options, timeout_secs)
    if choice is None:
        return JSONResponse({"error": "Approval backend unavailable"}, status_code=503)
    return JSONResponse({"choice": choice}, status_code=200)


# Agent containers are automated (no human at Claude Code), so MCP elicitation
# will always be instantly declined — skip it and go straight to WhatsApp polls
# via nanoclaw's approvals API.
#
# Batch approval: dedupe by (server, tool, risk) — not args — so approving one
# gog_gmail_trash covers the entire batch for approval_ttl_seconds.
approval_middleware = ApprovalMiddleware(
    mode="annotated",
    server_name="claw-host-bridge",
    approval_ttl_seconds=float(APPROVAL_TIMEOUT),
    approval_dedupe_key_fields=["server", "tool", "risk"],
    engine=WhatsAppEngine(
        bridge_url=_SELF_BRIDGE_URL,
        timeout=float(APPROVAL_TIMEOUT + 15),
        api_mode="whatsapp_poll",
        transport_policy=TransportPolicy(
            retry_attempts=2,
            retry_initial_backoff_seconds=0.5,
            retry_max_backoff_seconds=2.0,
            retry_backoff_multiplier=2.0,
            on_timeout="deny",
            on_transport_error="deny",
            allow_insecure_http=True,
            allowed_hosts=frozenset({"127.0.0.1", "localhost"}),
        ),
    ),
)
mcp.add_middleware(approval_middleware)


async def _reload_actions() -> None:
    global _actions
    try:
        result = await _olivetin("GetDashboard", {"title": "Home"})
        new_actions: dict[str, dict] = {}
        _walk_dashboard(result.get("dashboard", {}).get("contents", []), new_actions)

        if not new_actions:
            result = await _olivetin("GetDashboard", {})
            _walk_dashboard(result.get("dashboard", {}).get("contents", []), new_actions)

        async with _actions_lock:
            _actions = new_actions

        await _rebuild_action_tools(new_actions)

        # Sync tool metadata (annotations, decorator hints) into the middleware
        # so it knows which actions require approval.
        await approval_middleware.register_from_server(mcp)

        print(f"[host-bridge] Loaded {len(_actions)} OliveTin actions: {sorted(_actions.keys())}", flush=True)
        if len(_actions) > 500:
            print(f"[host-bridge] WARNING: {len(_actions)} actions loaded — unusually large", flush=True)
        if not _actions:
            print(
                "[host-bridge] WARNING: No OliveTin actions loaded — all action calls will fail "
                "until OliveTin is reachable and configured.",
                flush=True,
            )
    except Exception as exc:
        print(f"[host-bridge] WARNING: Failed to load OliveTin actions: {exc}", flush=True)


async def _refresh_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=300)
            break
        except TimeoutError:
            await _reload_actions()


# ─── Static tools ─────────────────────────────────────────────────────────────


@mcp.tool(
    name="brave_search",
    description=(
        "Search the web using Brave Search API. Returns top results with title, URL, description. "
        "No approval needed — read-only."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def brave_search(query: str, count: int = 10) -> str:
    if not BRAVE_API_KEY:
        return "Brave Search not configured (missing brave_api_key secret)."

    count = min(int(count), 20)
    url = (
        "https://api.search.brave.com/res/v1/web/search"
        f"?q={urllib.parse.quote(query)}&count={count}&safesearch=off"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Subscription-Token": BRAVE_API_KEY,
        },
    )

    try:
        loop = asyncio.get_event_loop()

        def _fetch() -> dict:
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"HTTP {exc.code}: {body}")

        data = await loop.run_in_executor(None, _fetch)
        results = data.get("web", {}).get("results", [])
        if not results:
            return "No results found."

        lines = [
            f"**{r.get('title', '')}**\n{r.get('url', '')}\n{r.get('description', '')}"
            for r in results
        ]
        return "\n\n".join(lines)
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
        "Render a Mermaid diagram as a PNG image via mermaid.ink (online). "
        "Returns /tmp/diagram_<hash>.png path. Use wacli to send it to WhatsApp. "
        "No approval needed."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def render_diagram(spec: str) -> str:
    spec = spec.strip()
    if not spec:
        return "Error: spec is empty."
    if re.search(r"(file://|javascript:|data:text/html)", spec, re.I):
        return "Error: diagram spec contains disallowed content."

    encoded = base64.urlsafe_b64encode(spec.encode()).decode().rstrip("=")
    url = f"https://mermaid.ink/img/{encoded}?type=png"

    try:
        loop = asyncio.get_event_loop()

        def _fetch() -> bytes:
            req = urllib.request.Request(url, headers={"User-Agent": "nanoclaw-host-bridge/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()

        data = await loop.run_in_executor(None, _fetch)
        digest = hashlib.md5(spec.encode()).hexdigest()[:8]
        out = f"/tmp/diagram_{digest}.png"
        with open(out, "wb") as f:
            f.write(data)
        asyncio.create_task(_cleanup(out))
        return out
    except Exception as exc:
        return f"render_diagram error: {exc}"


@mcp.tool(
    name="render_chart",
    description=(
        "Render a Chart.js chart as a PNG image via quickchart.io (online). "
        "Returns /tmp/chart_<hash>.png path. Use wacli to send it to WhatsApp. "
        "No approval needed."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def render_chart(
    config: dict,
    width: int = 500,
    height: int = 300,
    background_color: str = "white",
) -> str:
    if not config:
        return "Error: config is empty."

    payload = json.dumps(
        {
            "chart": config,
            "width": int(width),
            "height": int(height),
            "backgroundColor": background_color,
            "format": "png",
        }
    ).encode()

    try:
        loop = asyncio.get_event_loop()

        def _fetch() -> bytes:
            req = urllib.request.Request(
                "https://quickchart.io/chart",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()

        data = await loop.run_in_executor(None, _fetch)
        digest = hashlib.md5(json.dumps(config, sort_keys=True).encode()).hexdigest()[:8]
        out = f"/tmp/chart_{digest}.png"
        with open(out, "wb") as f:
            f.write(data)
        asyncio.create_task(_cleanup(out))
        return out
    except Exception as exc:
        return f"render_chart error: {exc}"


@mcp.tool(
    name="list_olivetin_actions",
    description=(
        "List all available OliveTin actions with IDs, titles, parameters, and read-only status. "
        "Call this to discover what commands are available before calling them."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def list_olivetin_actions() -> str:
    async with _actions_lock:
        actions_snapshot = list(_actions.values())

    catalog = [
        {
            "id": action["bindingId"],
            "title": action.get("title", ""),
            "read_only": _is_read_only(action),
            "args": [
                {
                    "name": arg["name"],
                    "type": arg.get("type", "string"),
                    "default": arg.get("defaultValue", ""),
                }
                for arg in action.get("arguments", [])
            ],
        }
        for action in actions_snapshot
    ]
    return json.dumps(catalog, indent=2)


@mcp.tool(
    name="get_execution_logs",
    description=(
        "Get recent OliveTin command execution history. "
        "Optionally filter by action_id. No approval needed."
    ),
    annotations=mt.ToolAnnotations(readOnlyHint=True),
)
async def get_execution_logs(action_id: str = "") -> str:
    try:
        if action_id:
            result = await _olivetin("GetActionLogs", {"actionId": action_id})
        else:
            result = await _olivetin("GetLogs", {})
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"GetLogs error: {exc}"


# ─── MCP Resources ────────────────────────────────────────────────────────────
# The LLM can read these before calling tools to understand what's available
# and what has run. Exposed under the olivetin:// URI scheme.


@mcp.resource(
    "olivetin://actions",
    name="OliveTin Actions Catalog",
    description=(
        "Full catalog of available OliveTin actions: IDs, titles, argument schemas, "
        "read-only status, and whether approval is required. Read this before calling an action."
    ),
    mime_type="application/json",
)
async def resource_olivetin_actions() -> str:
    async with _actions_lock:
        actions_snapshot = list(_actions.values())

    catalog = [
        {
            "id": action["bindingId"],
            "title": action.get("title", ""),
            "description": action.get("description") or "",
            "read_only": _is_read_only(action),
            "approval_required": not _is_read_only(action),
            "timeout_secs": action.get("timeout", 60),
            "args": [
                {
                    "name": arg["name"],
                    "type": arg.get("type", "string"),
                    "description": arg.get("description") or arg.get("title") or "",
                    "default": arg.get("defaultValue", ""),
                    "required": not bool(arg.get("defaultValue", "")),
                    "choices": [
                        c["value"] if isinstance(c, dict) else c
                        for c in arg.get("choices", [])
                    ],
                }
                for arg in action.get("arguments", [])
            ],
        }
        for action in actions_snapshot
    ]
    return json.dumps(catalog, indent=2)


@mcp.resource(
    "olivetin://actions/{action_id}/help",
    name="OliveTin Action Help",
    description=(
        "Full documentation for a specific OliveTin action: arguments, types, defaults, "
        "approval requirements, and optional prompt template."
    ),
    mime_type="text/plain",
)
async def resource_olivetin_action_help(action_id: str) -> str:
    async with _actions_lock:
        action = _actions.get(action_id)

    if not action:
        return (
            f"Unknown action '{action_id}'.\n"
            "Read the resource olivetin://actions to see the full catalog."
        )

    title = action.get("title", action_id)
    display = title.replace("[read-only]", "").replace("[READ-ONLY]", "").strip()
    read_only = _is_read_only(action)
    timeout = action.get("timeout", 60)
    description = action.get("description") or ""
    args = action.get("arguments", [])

    lines = [
        f"# {display}",
        "",
        f"**ID**: `{action_id}`",
        f"**Approval required**: {'No — read-only action' if read_only else 'Yes — triggers elicitation dialog, falls back to WhatsApp poll'}",
        f"**Timeout**: {timeout}s",
    ]
    if description:
        lines += ["", "## Description", "", description]

    lines += ["", "## Arguments", ""]
    if args:
        for arg in args:
            name = arg["name"]
            arg_type = arg.get("type", "string")
            default = arg.get("defaultValue", "")
            desc = arg.get("description") or arg.get("title") or ""
            choices = [c["value"] if isinstance(c, dict) else c for c in arg.get("choices", [])]
            lines.append(f"### `{name}` ({arg_type})")
            if desc:
                lines.append(desc)
            lines.append(f"- Required: {'Yes' if not default else f'No (default: {default!r})'}")
            if choices:
                lines.append(f"- Choices: {', '.join(repr(c) for c in choices)}")
            lines.append("")
    else:
        lines += ["No arguments.", ""]

    # Prompt template embedded in OliveTin action metadata (custom field).
    # Add promptTemplate or prompt_template to an action in OliveTin YAML to
    # expose a named MCP prompt that the LLM can use for formatted output.
    prompt_tpl = action.get("promptTemplate") or action.get("prompt_template") or ""
    if prompt_tpl:
        lines += ["## Prompt Template", "", prompt_tpl, ""]

    return "\n".join(lines)


@mcp.resource(
    "olivetin://logs",
    name="OliveTin Execution Logs",
    description="Recent execution history across all OliveTin actions. Useful for auditing what has run.",
    mime_type="application/json",
)
async def resource_olivetin_logs() -> str:
    try:
        result = await _olivetin("GetLogs", {})
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
    async with _actions_lock:
        known = action_id in _actions

    if not known:
        return json.dumps(
            {"error": f"Unknown action '{action_id}'. Read olivetin://actions to see the catalog."}
        )

    try:
        result = await _olivetin("GetActionLogs", {"actionId": action_id})
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps({"error": f"GetActionLogs failed: {exc}"})


# ─── MCP Prompts ──────────────────────────────────────────────────────────────
# Two layers:
#   1. Static olivetin_run prompt — compose a prompt for any action + output format.
#   2. Dynamic per-action prompts — registered when an action declares a
#      `promptTemplate` (or `prompt_template`) field in its OliveTin metadata.
#      These are named `olivetin_{action_id}` and re-registered on each reload.


@mcp.prompt(
    name="olivetin_run",
    description=(
        "Compose a ready-to-send prompt for running an OliveTin action and formatting "
        "its output. Uses the action's promptTemplate if declared, otherwise generates "
        "a default. output_format: plain | json | markdown | summary"
    ),
)
async def prompt_olivetin_run(action_id: str, output_format: str = "plain") -> str:
    async with _actions_lock:
        action = _actions.get(action_id)

    if not action:
        return (
            f"Action '{action_id}' not found. "
            "Read the resource olivetin://actions to discover available actions."
        )

    title = (
        action.get("title", action_id)
        .replace("[read-only]", "")
        .replace("[READ-ONLY]", "")
        .strip()
    )
    args_desc = ", ".join(
        f"`{a['name']}` ({a.get('type', 'string')})" for a in action.get("arguments", [])
    )
    format_instructions = {
        "json": "Return the result as a JSON object with `output`, `exit_code`, and `action_id` keys.",
        "markdown": "Format the output in a Markdown code block with appropriate syntax highlighting.",
        "summary": "Summarise the result in 1–3 sentences, calling out any warnings or errors.",
    }.get(output_format, "Return the raw output text as-is.")

    # Prefer a custom template declared in OliveTin YAML action metadata.
    prompt_tpl = action.get("promptTemplate") or action.get("prompt_template") or ""
    if prompt_tpl:
        return f"{prompt_tpl}\n\n{format_instructions}"

    base = f"Run the OliveTin action `{action_id}` ({title})."
    if args_desc:
        base += f" Required arguments: {args_desc}."
    return f"{base}\n\n{format_instructions}"


def _build_action_prompt(action: dict) -> str | None:
    """
    Register a per-action FastMCP prompt when the action declares a promptTemplate.
    The template may use {arg_name} placeholders matching the action's arguments.
    Returns the registered prompt name, or None if no template is declared.
    """
    template = action.get("promptTemplate") or action.get("prompt_template") or ""
    if not template:
        return None

    action_id = action["bindingId"]
    title = (
        action.get("title", action_id)
        .replace("[read-only]", "")
        .replace("[READ-ONLY]", "")
        .strip()
    )
    prompt_name = f"olivetin_{action_id}"
    _template = template

    async def _prompt_fn(**kwargs: str) -> str:
        text = _template
        for key, value in kwargs.items():
            text = text.replace(f"{{{key}}}", str(value))
        return text

    _prompt_fn.__name__ = prompt_name
    _prompt_fn.__doc__ = f"Prompt template for OliveTin action '{title}'."

    try:
        mcp.add_prompt(_prompt_fn, name=prompt_name, description=f"Prompt template for '{title}'.")
        return prompt_name
    except TypeError:
        # Older FastMCP: positional only
        try:
            mcp.add_prompt(_prompt_fn)
            return prompt_name
        except Exception as exc:
            print(f"[host-bridge] Could not register prompt {prompt_name}: {exc}", flush=True)
            return None
    except Exception as exc:
        print(f"[host-bridge] Could not register prompt {prompt_name}: {exc}", flush=True)
        return None


# ─── Entry point ──────────────────────────────────────────────────────────────


if __name__ == "__main__":
    mcp.run(
        transport="sse",
        host="0.0.0.0",
        port=PORT,
        path="/sse",
        log_level="warning",
        show_banner=False,
    )
