# Architecture

This document describes the high-level architecture of olivetin-mcp for contributors and users who want to understand the codebase.

## Overview

```
┌──────────────┐    stdio/HTTP    ┌──────────────────┐      JSON RPC      ┌──────────────┐
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
- Retries known read RPCs with `tenacity` (3 attempts, exponential backoff); writes and unknown RPCs get one attempt
- Single method: `call(endpoint, payload)` → `dict`

### `ActionRegistry`

**Role**: Action catalog manager and MCP tool/prompt lifecycle.

- Fetches `GetDashboard` from OliveTin, tolerates a null named dashboard, and recursively walks the default tree
- Classifies actions as read-only (`[read-only]` title prefix or `ro_` binding ID) vs destructive
- Builds Pydantic models from OliveTin `ActionArgument` proto; forwards validated defaults and normalized string values, rejects unknown inputs and invalid choices
- Registers/unregisters MCP tools and prompts dynamically on catalog refresh; validates new tools before replacing the working catalog; reserves built-in names
- Manages per-action rate limiters via `aiolimiter`

### `ApprovalManager`

**Role**: Two-stage human approval gate for destructive actions.

- **Stage 1 — MCP Elicitation**: Uses `ctx.elicit()` for inline dialog in Claude Desktop/Code. Times out after `ELICITATION_TIMEOUT_SECS` (default 30s).
- **Modern protocol**: On MCP 2026-07-28, returns `InputRequiredToolResult` and consumes the continuation's single-use, expiring nonce before execution. State is bound to the exact tool/arguments; FastMCP seals it on the wire. These connections require an elicitation-capable client.
- **Stage 2 — URL-based Pending**: Creates a cryptographic token, serves an HTML approval UI at `/pending/<token>`. Blocks the tool call until human clicks Approve/Deny or timeout.
- Exposes pending queue via HTTP routes and MCP resources. HTTP routes run only in network transports, not stdio.

These are alternative approval paths: an accepted elicitation executes immediately. The client is trusted, and URL tokens do not authenticate a human. See [SECURITY.md](SECURITY.md) for deployment boundaries.

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
| `get_execution_queue` | Inspect concurrency groups and queued work |
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
9. **Retry resilience**: `tenacity` with exponential backoff for known reads only; uncertain writes are never automatically repeated

## File Layout

```
main.py                 # All server code (single-file architecture)
olivetin_mcp.py         # Re-export for pip installability
tests/test_main.py      # Existing unit suite
tests/test_regressions.py # HTTP, MCP, validation, catalog, and approval regressions
tests/test_integration.py # Opt-in real OliveTin/stdio/HTTP verification
skills/                 # Reusable operator and action-author instructions
Dockerfile              # Hardened production container
pyproject.toml          # Package metadata, dependencies, tool config
requirements.txt        # Docker build dependencies
uv.lock                 # Exact dependency resolution; requirements.txt exports hashes
.env.example            # Configuration reference
```
