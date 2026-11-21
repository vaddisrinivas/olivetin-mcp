# Changelog

## [0.2.0] - Unreleased

### Added
- Streamable HTTP on `/mcp` via `--http`; legacy `--sse` remains available.
- Modern MCP input-required approvals alongside legacy elicitation, with expiring single-use state bound to the requested tool and arguments.
- `get_execution_queue` for OliveTin concurrency groups and queued work.
- Community `olivetin-operator` and `olivetin-action-author` skills.
- Real stdio/HTTP integration tests against an isolated OliveTin 3000.22.0 demo.
- Python 3.14 CI coverage, locked CI installs, hashed container requirements, and container smoke tests.

### Fixed
- `StartAction` now sends protobuf `bindingId`; synchronous starts retain `actionId`.
- Write RPCs no longer retry uncertain network responses, preventing automatic duplicate execution.
- Null dashboards fall back to default discovery; invalid refreshes retain the working catalog.
- Defaults, choices, booleans, and unknown arguments validate consistently for sync/async starts.
- `ascii_identifier` accepts dots, matching current OliveTin documentation.
- Dynamic prompts register, render, and refresh through current FastMCP APIs.
- Approval denials return protocol-valid error results; expired approvals cannot be accepted.
- Action IDs cannot shadow built-in tools; structured logs use stderr to preserve stdio.
- Brave Search supports its documented environment key.

### Changed
- FastMCP 4.2.3 / MCP SDK 2 and refreshed compatible dependencies; Python 3.11+ retained.
- Docker defaults to Streamable HTTP; change the command to `--sse` to preserve older clients.
- Host network listeners default to loopback. Docker listens on container interfaces; Compose publishes loopback only.
- Python 3.14.9 and OliveTin 3000.22.0 container images are pinned by version and digest.
- Container example includes a working config mount, bounded scratch space, and rotated bridge logs.
- Security documentation now states client trust, URL token limits, missing inbound authentication, and external renderer data flow.

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Community disclaimer and "Trust Gap" motivation section in README
- Risks and Limitations section in README
- Related Projects section (OliveTin, MCP, FastMCP, OpenClaw links)
- Background / origin story section
- `GOVERNANCE.md` — maintainer roles, decision-making process, co-maintainer path
- `ARCHITECTURE.md` — codebase overview with component diagram and request flow
- `CODEOWNERS` — automated PR routing to maintainers
- `docker-compose.yml` — example setup with OliveTin for local development
- `Makefile` — common dev commands (`make test`, `make ci`, `make lint`, etc.)
- `.pre-commit-config.yaml` — ruff lint + format on every commit
- Dependabot configuration for dependency and GitHub Actions updates
- PyPI publish workflow (GitHub Actions → Trusted Publisher)
- Docker build job in CI
- mypy type checking in CI
- Coverage threshold enforcement (`--cov-fail-under=95`)
- Security issue template (routes to email, not public issues)
- Security headers on all HTML responses (`X-Frame-Options`, `CSP`, `X-Content-Type-Options`, `Referrer-Policy`)
- SSRF protection expanded to block `gopher://`, `ftp://`, `dict://`, `ldap://`, `tftp://`, `ssh://`
- Threat model in `SECURITY.md`
- FAQ / Troubleshooting section in README
- "Why OliveTin-MCP?" section and comparison table in README
- Example OliveTin action YAML in README

### Fixed
- Error messages no longer leak OliveTin response bodies or internal details
- Mutable default argument `dict = {}` in `start_action_async` replaced with `None`
- Duplicate `import base64 as _b64` removed
- Temp files use `tempfile.mkstemp()` instead of predictable `/tmp` paths
- SafeSearch default changed from `off` to `moderate` for Brave Search

### Changed
- `asyncio.TimeoutError` replaced with built-in `TimeoutError` (Python 3.11+)
- PR template now includes changelog and type check reminders

## [2.0.0] - 2025-01-01

### Added
- Full OliveTin proto 1:1 API coverage:
  - `GetDashboard`, `StartActionAndWait`, `StartAction`, `RestartAction`
  - `GetLogs`, `GetActionLogs`, `ExecutionStatus`, `KillAction`
  - `ValidateArgumentType`, `WhoAmI`, `Init`, `GetActionBinding`
  - `GetEntities`, `GetEntity`, `GetReadyz`
- Three authentication modes: reverse-proxy headers, JWT bearer, HTTP Basic
- Built-in approval chain: MCP native elicitation + URL-based pending
  - For v1 users: approval is now built-in; remove external nanoclaw proxy
- Rate limiting per action via `aiolimiter`
- Structured logging with `structlog`
- Retry logic with exponential backoff via `tenacity`
- `/healthz` health check endpoint
- `brave_search`, `render_diagram`, `render_chart` static tools
- MCP resources for action catalog, logs, and pending approvals
- MCP prompts for action execution
- 100% test coverage (209 tests)
- Hardened Dockerfile (non-root, no shells, no pip, dropped capabilities)
- pip-installable as `olivetin-mcp`

### Changed
- Rewritten from scratch for FastMCP 3.x
- Approval flow no longer requires external proxy (built-in)

## [1.0.0] - 2024-12-01

### Added
- Initial release
- OliveTin action execution via MCP tools
- WhatsApp-based approval via nanoclaw
