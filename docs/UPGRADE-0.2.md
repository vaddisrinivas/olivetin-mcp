# Community upgrade verification

Date: 2026-11-20. Target: unreleased 0.2.0, based on public commit `bb5efd5`.

## Changes

- Upgraded to FastMCP 4.2.3 / MCP SDK 2 and current compatible dependencies; exact resolution in `uv.lock`, hash-checked production dependencies in `requirements.txt`.
- Added Streamable HTTP; retained stdio and legacy SSE. Host listeners and Compose-published ports default to loopback.
- Added modern MCP input-required approval and retained legacy elicitation. Modern approvals bind exact tools/arguments to expiring, single-use continuation state; invalid or replayed decisions deny execution.
- Fixed async `StartAction` to use `bindingId`, consistent with the release-tagged protobuf.
- Limited network retries to known read RPCs, avoiding automatic duplicate action execution.
- Unified sync/async argument validation, forwarded defaults, enforced choices/unknown-field rejection, and normalized boolean wire values.
- Fixed null-dashboard discovery, prompt registration/render/removal, stale-catalog retention, static-tool name collisions, stdio logging, and protocol-invalid denial responses.
- Added queue inspection, harmless example actions, two reusable agent skills, reproducible CI, and Docker smoke-test configuration.
- Replaced unsupported coverage/security claims with explicit behavior and deployment limits.

## Verified locally

| Check | Observed result |
|-------|-----------------|
| Python 3.11.4 | 282 passed, 2 opt-in integration tests skipped |
| Python 3.12.11 | 282 passed, 2 skipped |
| Python 3.13.14 | 282 passed, 2 skipped |
| Python 3.14.9 | 282 passed, 2 skipped; 99.36% `main.py` coverage |
| Ruff lint/format, mypy | Passed |
| GitHub Actions syntax / shell checks | Passed with actionlint |
| Skill validation | Both SKILL.md files passed skill-creator validation |
| Secret scan | No findings in changed tracked content or new skills/examples |
| OliveTin configuration syntax | `0 configuration issues found` |
| Real OliveTin 3000.22.0 | Release archive checksum verified; isolated native ARM64 process used |
| Real MCP transports | Stdio, Streamable HTTP, and SSE discovery/execution passed |
| Real asynchronous start | Client approval, `StartAction`, tracking, polling, output, and exit code passed |
| Package build | Wheel and source distribution built successfully |
| Installed wheel | Tested outside the source directory with runtime-only, hash-checked dependencies |
| Archive contents | Skills included in source distribution; local environment symlinks excluded |
| Requirements export | Pinned CI uv version regenerates identical requirements |

The ordinary test suite skips integration tests unless `OLIVETIN_TEST_URL` is set. Real integration tests were also run separately against the bundled harmless actions; no production action catalog was used.

## Limits and migration

- **Docker execution requires hosted CI:** no Docker CLI/daemon is available locally. Image tags and digests were verified against registry metadata. The PR's `docker` check builds and runs hardened containers; consult its result separately from the local evidence above.
- Public historical CI metadata identifies failed type-check and Python test steps. Full historical logs are expired (GitHub HTTP 410); current local tests provide fresh evidence instead.
- Docker now starts `/mcp` by default. Legacy clients need the `--sse` command override and `/sse` URL.
- Network listeners now default to loopback. Remote deployments must configure an explicit bind address and authenticated TLS ingress.
- Strict argument validation rejects undeclared fields and invalid choices previously ignored by the bridge.
- Approval is client-trusted. URL tokens do not establish human identity; independent approval needs separately authenticated human ingress and agent isolation from approval POST routes.
- Pending approval state is process local. Use one bridge process; restarts invalidate approvals.
- Version 0.2.0 remains unreleased. Merging this change does not publish a package/image or change a live deployment.

## Sources and reproduction

- [OliveTin API overview](https://docs.olivetin.app/api/intro.html)
- [OliveTin 3000.22.0 protobuf](https://github.com/OliveTin/OliveTin/blob/3000.22.0/proto/olivetin/api/v1/olivetin.proto)
- [FastMCP elicitation across protocol versions](https://gofastmcp.com/docs/servers/elicitation)
- [Development and integration commands](../README.md#development)

Official OliveTin `llm.txt` and `llm-full.txt` were downloaded for research. Raw runtime/test evidence is private and excluded from source and distribution artifacts; it can contain execution output and local paths.
