# Architecture

This document describes the high-level architecture of olivetin-mcp for contributors and users who want to understand the codebase.

## Overview

```
┌──────────────┐       SSE        ┌──────────────────┐      REST API      ┌──────────────┐
│  MCP Client  │ <──────────────> │   olivetin-mcp   │ ──────────────────> │   OliveTin   │
│ (Claude, etc)│    port 9003     │  (this server)    │    port 1337       │  (host)      │
└──────────────┘                  └──────────────────┘                     └──────────────┘
                                         │
                                         v
                                  ┌──────────────┐
                                  │  Approval UI  │
                                  │ /pending/:tok │
                                  └──────────────┘
```

## Core Components

All code lives in `main.py` (~1500 lines). The architecture is organized into four main classes:

### `OliveTinClient`

**Role**: HTTP transport layer to OliveTin's REST API.

- Owns the `httpx.AsyncClient` connection pool
- Constructs auth headers (3 modes: header, JWT, Basic — composable)
- Wraps all API calls with `tenacity` retry (3 attempts, exponential backoff)
- Single method: `call(endpoint, payload)` → `dict`

### `ActionRegistry`

**Role**: Action catalog manager and MCP tool/prompt lifecycle.

- Fetches `GetDashboard` from OliveTin, recursively walks the dashboard tree
- Classifies actions as read-only (`[read-only]` title prefix or `ro_` binding ID) vs destructive
- Builds Pydantic models from OliveTin `ActionArgument` proto for input validation
- Registers/unregisters MCP tools and prompts dynamically on catalog refresh
- Manages per-action rate limiters via `aiolimiter`

### `ApprovalManager`

**Role**: Two-stage human approval gate for destructive actions.

- **Stage 1 — MCP Elicitation**: Uses `ctx.elicit()` for inline dialog in Claude Desktop/Code. Times out after `ELICITATION_TIMEOUT_SECS` (default 30s).
- **Stage 2 — URL-based Pending**: Creates a cryptographic token, serves an HTML approval UI at `/pending/<token>`. Blocks the tool call until human clicks Approve/Deny or timeout.
- Exposes pending queue via HTTP routes and MCP resources.

### `_ApprovalMiddleware`

**Role**: FastMCP middleware that intercepts every `call_tool` request.

- Checks if the tool is in `readonly_tool_names` → pass through
- Otherwise, runs the two-stage approval chain
- Returns denial message if not approved

## Request Flow

```
MCP Client calls tool "deploy_app"
        │
        v
_ApprovalMiddleware.on_call_tool()
        │
        ├─ Is "deploy_app" read-only? → No
        │
        ├─ Stage 1: try_elicitation()
        │   ├─ User approves inline → execute tool
        │   ├─ User denies inline → return denial
        │   └─ Timeout/unsupported → fall through
        │
        ├─ Stage 2: pending_url_approval()
        │   ├─ Create token, serve /pending/<token>
        │   ├─ Human visits URL, clicks Approve → execute tool
        │   ├─ Human clicks Deny → return denial
        │   └─ Timeout → return denial
        │
        v
ActionRegistry.run_action()
        │
        v
OliveTinClient.call("StartActionAndWait", ...)
```

## Static Tools

Beyond dynamic OliveTin action tools, the server registers static tools:

| Tool | Purpose |
|------|---------|
| `list_olivetin_actions` | Catalog discovery |
| `get_execution_logs` | Execution history |
| `get_execution_status` | Poll running action |
| `kill_action` | Emergency stop |
| `reload_actions` | Force catalog refresh |
| `brave_search` | Web search (optional) |
| `render_diagram` | Mermaid diagram rendering |
| `render_chart` | Chart.js rendering |
| `start_action_async` | Non-blocking action start |
| `restart_action` | Re-run previous execution |
| OliveTin RPC tools | `validate_argument`, `whoami`, `get_server_info`, etc. |

## MCP Resources

Resources provide read-only data access:

- `olivetin://actions` — Full action catalog (JSON)
- `olivetin://actions/{id}/help` — Per-action documentation
- `olivetin://logs` — Recent execution history
- `olivetin://logs/{id}` — Action-specific logs
- `olivetin://pending` — Pending approval queue

## Security Layers

1. **Container hardening**: Non-root user, stripped shells/tools, no pip at runtime
2. **Input validation**: Pydantic schema validation on all action arguments
3. **SSRF protection**: Diagram specs checked for disallowed protocols
4. **XSS prevention**: Jinja2 `autoescape=True` on all HTML templates
5. **Approval gating**: Destructive actions require human approval
6. **Cryptographic tokens**: `secrets.token_urlsafe(16)` for approval URLs
7. **Security headers**: `X-Frame-Options`, `CSP`, `X-Content-Type-Options` on all HTML responses
8. **Rate limiting**: Per-action rate limits via `aiolimiter`
9. **Retry resilience**: `tenacity` with exponential backoff on transient errors

## File Layout

```
main.py                 # All server code (single-file architecture)
olivetin_mcp.py         # Re-export for pip installability
tests/test_main.py      # 200+ tests, ~100% coverage
Dockerfile              # Hardened production container
pyproject.toml          # Package metadata, dependencies, tool config
requirements.txt        # Docker build dependencies
.env.example            # Configuration reference
```
