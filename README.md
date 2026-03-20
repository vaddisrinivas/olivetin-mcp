# nanoclaw-host-bridge

FastMCP SSE server that exposes [OliveTin](https://docs.olivetin.app) actions as MCP tools to nanoclaw agent containers, with WhatsApp-based approval gating for destructive operations.

## What it does

- Runs as a Docker container on your local network
- Exposes OliveTin actions (shell commands, scripts) as MCP tools
- Routes approval requests to nanoclaw's WhatsApp poll API
- Hardened: non-root, no shells, no pip, read-only filesystem, dropped capabilities

## Requirements

- [OliveTin](https://docs.olivetin.app) running on the host at port 1337
- nanoclaw running and connected to WhatsApp (for approval polls)

## Setup

Used as a git submodule inside [nanoclaw](https://github.com/vaddisrinivas/nanoclaw).
The `configure.sh` script in nanoclaw handles setup automatically.

To build standalone:
```bash
docker build -t nanoclaw-host-bridge .
```

## Configuration

Environment variables (set via docker-compose in nanoclaw):

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST_BRIDGE_PORT` | `9003` | Port for the MCP SSE server |
| `OLIVETIN_URL` | `http://host.docker.internal:1337` | OliveTin API URL |
| `NANOCLAW_APPROVALS_URL` | `http://nanoclaw:3002` | nanoclaw approval API |
| `APPROVAL_TIMEOUT_SECS` | `120` | Seconds to wait for WhatsApp vote |
| `SECRETS_DIR` | `/run/secrets` | Path to Docker secrets mount |
