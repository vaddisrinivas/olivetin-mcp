# olivetin-mcp

[![CI](https://github.com/vaddisrinivas/olivetin-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/vaddisrinivas/olivetin-mcp/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)]()
[![PyPI](https://img.shields.io/pypi/v/olivetin-mcp.svg)](https://pypi.org/project/olivetin-mcp)

A hardened [MCP](https://modelcontextprotocol.io/) server that exposes [OliveTin](https://docs.olivetin.app) actions as tools, with built-in human-in-the-loop approval for destructive operations.

## Why OliveTin-MCP?

[OliveTin](https://docs.olivetin.app) lets you define shell commands as safe, parameterized actions with a web UI. **olivetin-mcp** bridges those actions into the [Model Context Protocol](https://modelcontextprotocol.io/) so Claude (or any MCP client) can discover and execute them — with automatic human approval gates for anything destructive.

**Without this bridge**: Claude can't see or trigger your OliveTin actions. You'd need to manually copy-paste commands or build custom integrations.

**With this bridge**: Claude discovers all your actions automatically, validates arguments, and asks for human approval before running anything dangerous.

| Approach | Pros | Cons |
|----------|------|------|
| **Direct shell via Claude** | Simple, no setup | No action discovery, no approval gates, no audit trail |
| **OliveTin Web UI alone** | Built-in approval UI | No AI integration, manual operation only |
| **OliveTin API directly** | Full control | Manual auth, no MCP schema, no standardized discovery |
| **olivetin-mcp** (this) | Auto-discovery, approval chain, hardened container, MCP standard | Requires MCP client + OliveTin |

## How It Works

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

1. **Discovers** OliveTin actions and registers each as an MCP tool
2. **Classifies** actions as read-only or destructive based on naming conventions
3. **Gates** destructive actions behind a two-step approval chain:
   - MCP native elicitation (inline dialog in Claude Code / Claude Desktop)
   - URL-based approval (human visits `/pending/<token>` to approve or deny)
4. **Executes** approved actions via the OliveTin REST API
5. **Exposes** resources for action catalogs, execution logs, and pending approvals

## Features

- Full OliveTin API coverage (actions, logs, execution status, kill)
- Three authentication modes (reverse-proxy headers, JWT, HTTP Basic)
- Built-in static tools: `brave_search`, `render_diagram`, `render_chart`
- Rate limiting per action
- Structured logging with `structlog`
- Retry with exponential backoff via `tenacity`
- Security headers on all HTML responses
- `/healthz` health check endpoint

## Requirements

- Python 3.11+
- [OliveTin](https://docs.olivetin.app) running and accessible

## Quick Start

### Docker (recommended)

```bash
docker build -t olivetin-mcp .
docker run -d \
  -p 9003:9003 \
  -e OLIVETIN_URL=http://host.docker.internal:1337 \
  olivetin-mcp
```

### Docker Compose

A `docker-compose.yml` is included for running olivetin-mcp alongside OliveTin:

```bash
docker compose up -d
```

### pip

```bash
pip install olivetin-mcp
python -m main
```

### From source

```bash
git clone https://github.com/vaddisrinivas/olivetin-mcp.git
cd olivetin-mcp
pip install -e ".[test]"
python main.py
```

## MCP Client Configuration

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "olivetin": {
      "url": "http://localhost:9003/sse"
    }
  }
}
```

### Claude Code

```bash
claude mcp add olivetin --transport sse http://localhost:9003/sse
```

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` to get started.

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST_BRIDGE_PORT` | `9003` | MCP SSE server port |
| `OLIVETIN_URL` | `http://host.docker.internal:1337` | OliveTin API URL |
| `APPROVAL_TIMEOUT_SECS` | `120` | Seconds to wait for approval decision |
| `APPROVAL_BASE_URL` | `""` | Public URL for approval links (e.g. `https://bridge.example.com`) |
| `ELICITATION_TIMEOUT_SECS` | `30` | MCP elicitation dialog timeout |
| `SECRETS_DIR` | `/run/secrets` | Docker secrets mount path |
| `ACTION_REFRESH_INTERVAL_SECS` | `300` | Action catalog refresh interval |

### Authentication

Pick **one** mode depending on your OliveTin setup:

**Reverse-proxy headers** (e.g. Authelia, Authentik):
```env
OLIVETIN_REMOTE_USER=serviceaccount
OLIVETIN_REMOTE_GROUPS=admins,operators
OLIVETIN_USER_HEADER=X-Remote-User      # optional, default shown
OLIVETIN_GROUPS_HEADER=X-Remote-Groups   # optional, default shown
```

**JWT bearer token**:
```env
OLIVETIN_JWT_TOKEN=eyJhbGci...
# or mount as /run/secrets/olivetin_jwt_token
```

**HTTP Basic auth**:
```env
OLIVETIN_BASIC_USER=admin
OLIVETIN_BASIC_PASS=changeme
```

## Security

This project is designed to be run as a hardened container:

- **Non-root** user (`bridge:10001`) with no login shell
- **Stripped attack surface**: all shells, network tools, and compilers removed
- **No runtime pip**: package manager removed after build
- **Read-only filesystem**: designed for `read_only: true` in Docker Compose
- **Dropped capabilities**: all Linux capabilities can be dropped
- **Human-in-the-loop**: destructive actions require explicit approval
- **XSS protection**: Jinja2 `autoescape=True` on all templates
- **Cryptographic tokens**: approval tokens via `secrets.token_urlsafe(16)`
- **Security headers**: `X-Frame-Options`, `CSP`, `X-Content-Type-Options` on all HTML responses
- **SSRF protection**: diagram specs checked for disallowed URI schemes

See [SECURITY.md](SECURITY.md) for vulnerability reporting and threat model.

## MCP Tools

### Dynamic (one per OliveTin action)

Each OliveTin action is registered as an MCP tool. Read-only actions (prefixed with `[read-only]` or `ro_` binding ID) execute immediately. All others go through the approval chain.

### Static

| Tool | Description |
|------|-------------|
| `list_olivetin_actions` | List all actions with schemas and approval status |
| `get_execution_logs` | Recent execution history |
| `get_execution_status` | Status of a running execution |
| `kill_action` | Kill a running execution |
| `reload_actions` | Force refresh the action catalog |
| `list_pending_approvals` | Show pending approval queue |
| `brave_search` | Web search via Brave Search API |
| `render_diagram` | Render Mermaid/PlantUML/D2 diagrams |
| `render_chart` | Render charts via QuickChart |

## MCP Resources

| URI | Description |
|-----|-------------|
| `olivetin://actions` | Full action catalog (JSON) |
| `olivetin://actions/{id}/help` | Detailed action documentation |
| `olivetin://logs` | Recent execution history |
| `olivetin://logs/{id}` | Action-specific logs |
| `olivetin://pending` | Pending approval queue |

## Example OliveTin Actions

Here's how OliveTin actions map to MCP tools:

```yaml
# In your OliveTin config.yaml
actions:
  # This action executes immediately (no approval) because of [read-only] prefix
  - title: "[read-only] Get Service Status"
    shell: docker ps --format 'table {{.Names}}\t{{.Status}}'

  # This action requires human approval before execution
  - title: Deploy Service
    shell: cd /srv/{{ service }} && docker compose pull && docker compose up -d
    arguments:
      - name: service
        type: ascii_identifier
        choices:
          - value: api
          - value: web
          - value: worker
```

## FAQ / Troubleshooting

**Q: Claude shows the action but gets "Could not reach OliveTin"**

1. Verify OliveTin is running: `curl http://your-olivetin:1337/api/GetDashboard -X POST -d '{}'`
2. If using Docker, ensure network connectivity (use `http://host.docker.internal:1337` on Docker Desktop, or the container network name in Docker Compose)
3. Check `OLIVETIN_URL` env var

**Q: Actions require approval but I want some to be read-only**

Prefix the action title with `[read-only]` or the binding ID with `ro_` in your OliveTin config. The bridge automatically classifies these as safe.

**Q: Approval links don't work / show "Not Found"**

Set `APPROVAL_BASE_URL` to the publicly reachable URL of the bridge (e.g. `http://192.168.1.10:9003`). The default `localhost` only works when the browser is on the same machine.

**Q: MCP client times out waiting for approval**

The default approval timeout is 120 seconds. Increase `APPROVAL_TIMEOUT_SECS` if needed. The elicitation dialog (inline in Claude) times out after 30 seconds (`ELICITATION_TIMEOUT_SECS`) before falling back to URL-based approval.

**Q: Actions aren't showing up in Claude**

1. Check the bridge logs for `actions_loaded` — it should show the count
2. Run the `reload_actions` tool to force a refresh
3. Verify OliveTin has actions configured and they aren't marked `hidden: true`

**Q: How do I use Brave Search?**

Mount the API key as a Docker secret at `/run/secrets/brave_api_key`, or set `BRAVE_API_KEY` in your environment. Get a key at [brave.com/search/api](https://brave.com/search/api/).

## Development

```bash
# Install all extras
make install

# Run all CI checks locally
make ci

# Or individually:
make test     # pytest with coverage
make lint     # ruff check
make type     # mypy
make format   # auto-format
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for more details, and [ARCHITECTURE.md](ARCHITECTURE.md) for codebase overview.

## License

[MIT](LICENSE)
