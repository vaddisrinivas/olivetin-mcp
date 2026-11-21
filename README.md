# olivetin-mcp

[![CI](https://github.com/vaddisrinivas/olivetin-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/vaddisrinivas/olivetin-mcp/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Expose predefined [OliveTin](https://docs.olivetin.app/) actions as [MCP](https://modelcontextprotocol.io/) tools. Discover actions, validate arguments, request approval, run actions, and inspect execution history from an MCP client.

Community maintained; not affiliated with or endorsed by OliveTin. Version 0.2 requires Python 3.11+ and FastMCP 4. The integration suite targets **OliveTin 3000.22.0**; compatibility with other releases must be checked.

## Quick start

With an existing OliveTin instance:

```bash
claude mcp add olivetin -e OLIVETIN_URL=http://localhost:1337 -- uvx --from git+https://github.com/vaddisrinivas/olivetin-mcp.git olivetin-mcp
```

This uses stdio. A client supporting MCP elicitation can ask for approval inline. A stdio process does not serve the HTTP approval page; use a custom approval backend or a network transport if elicitation is unavailable.

To run the bundled harmless demo:

```bash
git clone https://github.com/vaddisrinivas/olivetin-mcp.git
cd olivetin-mcp
docker compose up -d --build
```

OliveTin: `http://localhost:1337`. MCP: `http://localhost:9003/mcp`. Compose publishes both services on loopback only and mounts [the example configuration](examples/olivetin/config.yaml) explicitly. Replace that configuration with reviewed actions for your deployment. Runtime scratch space is a bounded tmpfs; the bridge requires no persistent volume.

```bash
claude mcp add olivetin --transport http http://localhost:9003/mcp
```

Use a release tag or commit in the Git source URL when you need a reproducible client installation. The unqualified Git URL follows the repository's default branch.

## Transports

```bash
uv sync --frozen --all-extras
uv run --frozen olivetin-mcp          # stdio
uv run --frozen olivetin-mcp --http   # Streamable HTTP, /mcp
uv run --frozen olivetin-mcp --sse    # legacy SSE, /sse
```

Network listeners default to `127.0.0.1:9003`. `HOST_BRIDGE_HOST` controls binding; Docker sets it to `0.0.0.0` inside the container. The Docker default changed from SSE to Streamable HTTP in 0.2. To keep SSE, override the container command with `["python3", "main.py", "--sse"]` and connect to `/sse`.

For a client supporting command-based MCP configuration:

```json
{
  "mcpServers": {
    "olivetin": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/vaddisrinivas/olivetin-mcp.git", "olivetin-mcp"],
      "env": {"OLIVETIN_URL": "http://localhost:1337"}
    }
  }
}
```

## Approval and security

Actions execute without approval only when their title starts with `[read-only]` or their ID starts with `ro_` (case insensitive). These are operator assertions about command behavior. All other action tools, asynchronous starts, and restarts request approval:

1. MCP elicitation asks the client for a decision.
2. Legacy unsupported or timed-out elicitation falls back to a URL at `/pending/<token>`.
3. Denial, cancellation, expiry, or missing approval prevents execution.

An accepted elicitation executes immediately; URL approval is a fallback, not a second factor. The bridge trusts its MCP client and has **no built-in inbound authentication**. Anyone able to reach an approval endpoint with its token can decide it, including an agent with unrestricted HTTP access. For independent human approval, use separately authenticated human ingress and prevent agent access to approval POST routes. Protect remote deployments with authenticated TLS ingress. OliveTin credentials authenticate the bridge to OliveTin; they do not authenticate MCP callers. See [SECURITY.md](SECURITY.md).

MCP 2026-07-28 clients use the current input-required approval flow; older connections use `ctx.elicit`. Modern approvals require a client that can answer elicitation requests. Their state is single use, expires after `ELICITATION_TIMEOUT_SECS`, and binds the exact tool and arguments. Changed, expired, replayed, or missing state denies execution. Pending approvals are process local: run one bridge process; restarting it invalidates approvals.

Unknown action arguments and invalid declared choices are rejected before execution. Defaults are validated and forwarded; booleans become lowercase strings. OliveTin remains authoritative for specialized types and authorization. Only known read RPCs retry network errors. A lost response to a start/restart can mean the action already ran: inspect logs before retrying manually.

The container runs as a non-root user, strips shell/package tools, and supports a read-only root filesystem and dropped capabilities. Container hardening does not prove an action is safe. Diagram/chart tools send content to external rendering services; avoid private content.

## Tools, resources, and prompts

Each discovered action becomes a tool with an input schema. Built-in names are reserved and cannot be shadowed by actions.

| Tool | Purpose |
|------|---------|
| `list_olivetin_actions` | Discover action IDs, arguments, and approval classification |
| `start_action_async` | Start an action; retain `executionTrackingId` |
| `get_execution_status` | Poll an execution |
| `get_execution_logs` | Read execution history, optionally by action |
| `get_execution_queue` | Inspect active actions and concurrency queues |
| `kill_action` | Stop an execution immediately; approval exempt |
| `restart_action` | Re-run an execution after approval |
| `reload_actions` | Refresh the action catalog |
| `list_pending_approvals` | Find approval requests |
| `whoami`, `get_server_info`, `readyz` | Diagnose upstream identity, version, and availability |
| `validate_argument`, `get_action_binding` | Inspect action metadata and validation |
| `get_entities`, `get_entity` | Inspect configured entities |
| `brave_search` | Optional search using a configured API key |
| `render_diagram`, `render_chart` | Render through mermaid.ink or quickchart.io; temporary PNG paths |

Resources: `olivetin://actions`, `olivetin://actions/{action_id}/help`, `olivetin://logs`, `olivetin://logs/{action_id}`, and `olivetin://pending`.

The `olivetin_run` prompt composes execution instructions. Per-action `promptTemplate` metadata, if supplied by a compatible upstream extension, creates additional prompts. Current OliveTin's Action protobuf does not expose that extension.

## Configuration

Configure through environment variables or mounted secrets. `.env.example` documents the original settings; the table below includes 0.2 additions. The Python CLI reads process environment, not a project `.env` automatically.

| Variable | Default | Purpose |
|----------|---------|---------|
| `OLIVETIN_URL` | `http://host.docker.internal:1337` | Upstream server URL; set explicitly outside Docker Desktop |
| `HOST_BRIDGE_HOST` | `127.0.0.1` | Network bind address; Docker image uses `0.0.0.0` |
| `HOST_BRIDGE_PORT` | `9003` | HTTP/SSE port |
| `APPROVAL_BASE_URL` | `http://localhost:<port>` when unset | Browser-accessible bridge URL |
| `APPROVAL_TIMEOUT_SECS` | `120` | URL approval timeout |
| `ELICITATION_TIMEOUT_SECS` | `30` | Inline approval timeout |
| `ACTION_REFRESH_INTERVAL_SECS` | `300` | Catalog refresh interval |
| `SECRETS_DIR` | `/run/secrets` | Secret file directory |
| `BRAVE_API_KEY` | unset | Optional key; alternatively mount `brave_api_key` under `SECRETS_DIR` |

Upstream authentication:

- Trusted proxy headers: `OLIVETIN_REMOTE_USER`, `OLIVETIN_REMOTE_GROUPS`; optional `OLIVETIN_USER_HEADER` / `OLIVETIN_GROUPS_HEADER` (defaults `X-Remote-User` / `X-Remote-Groups`). Configure OliveTin's trusted-header authentication explicitly.
- JWT: `OLIVETIN_JWT_TOKEN` or mounted `olivetin_jwt_token`.
- Basic: `OLIVETIN_BASIC_USER` and `OLIVETIN_BASIC_PASS`, for a supporting reverse proxy. If both JWT and Basic are configured, Basic takes precedence.

Never commit credentials, production action catalogs, or unredacted execution logs.

## Agent skills

Two reusable community skills are included:

- [olivetin-operator](skills/olivetin-operator/SKILL.md): discovery, approvals, execution tracking, and troubleshooting.
- [olivetin-action-author](skills/olivetin-action-author/SKILL.md): safe YAML authoring, stable IDs, argument constraints, and isolated checks.

Copy the desired skill directory into your client's skill directory. For Codex, that is typically `~/.codex/skills`; for Claude Code, `~/.claude/skills`. These are agent instructions, not additional host execution permissions or automatically installed server tools.

## Troubleshooting

- **No actions:** inspect `actions_loaded` / `actions_load_failed` in stderr or container logs. Unknown named dashboards can return `dashboard: null`; the bridge falls back to the default dashboard. Use `reload_actions` after configuration changes. A failed refresh retains the last working catalog.
- **Connection errors:** verify `OLIVETIN_URL`, upstream authentication, and `/healthz`. Use `readyz` to check OliveTin itself; `/healthz` only reports bridge liveness.
- **Approval links unavailable:** stdio has no HTTP listener. For HTTP/SSE, configure `APPROVAL_BASE_URL` to a human-accessible authenticated address. Timeout is denial.
- **Lost execution response:** read history/status before retrying; the server may have executed the request.
- **Logs for a bug report:** include versions, transport, and a redacted error excerpt. Action output and arguments may contain secrets.

## Development

```bash
make install   # locked test and lint dependencies
make ci        # lint, format, typing, >=95% coverage
make lock      # deliberate dependency upgrade + hashed requirements export
uv build       # wheel and source distribution
```

CI covers Python 3.11–3.14 and includes a Docker job that executes the safe example through real stdio and HTTP MCP clients. To run those integration tests locally against an isolated instance using the bundled example:

```bash
OLIVETIN_TEST_URL=http://127.0.0.1:1337 \
MCP_TEST_URL=http://127.0.0.1:9003/mcp \
uv run --frozen pytest tests/test_integration.py -v
```

Without these variables, integration tests skip. Do not point them at a personal or production action catalog.

API changes should follow [the official API overview](https://docs.olivetin.app/api/intro.html) and [release-tagged protobuf](https://github.com/OliveTin/OliveTin/blob/3000.22.0/proto/olivetin/api/v1/olivetin.proto). OliveTin provides [llm.txt](https://docs.olivetin.app/llm.txt) and [llm-full.txt](https://docs.olivetin.app/llm-full.txt) for documentation retrieval.

See [CONTRIBUTING.md](CONTRIBUTING.md), [ARCHITECTURE.md](ARCHITECTURE.md), and [CHANGELOG.md](CHANGELOG.md). [MIT license](LICENSE).
