---
name: olivetin-operator
description: Discover and run configured OliveTin actions through olivetin-mcp, inspect execution logs and queues, and diagnose bridge connectivity or approval failures.
---

Use `list_olivetin_actions` or `olivetin://actions` before selecting an action. Action IDs come from the catalog; do not invent shell commands, binding IDs, or arguments. Read `olivetin://actions/{action_id}/help` when an argument or default is unclear.

Run the action's named MCP tool for ordinary work. For long operations, use `start_action_async`, retain `executionTrackingId`, and poll `get_execution_status`. Async starts always require approval. Inspect `get_execution_queue` for concurrency or queued work, and `get_execution_logs` for completion evidence. `kill_action` stops a running execution immediately; use only when stopping it matches the user's intent.

Present approval requests to the human. Never visit an approval POST endpoint, approve your own action, change read-only naming, or treat an approval timeout as consent. Elicitation depends on a trusted MCP client; URL approval depends on a trusted network or separately authenticated ingress. Neither proves a human decision against a client that controls both paths.

After a network error during a start or restart, inspect execution history before doing anything else. The action may have run even when the response was lost; do not automatically retry.

For connection failures, check `/healthz` (bridge process), `readyz` (OliveTin), `whoami` (upstream identity), and `get_server_info` (upstream version). `/mcp` is Streamable HTTP; `/sse` requires starting the bridge with `--sse`. Stdio logs belong on stderr. `reload_actions` refreshes action discovery after configuration changes.

Treat logs as private: they may contain action output, usernames, arguments, and tracking IDs. Share only redacted excerpts necessary to explain a failure.

Sources: [OliveTin API](https://docs.olivetin.app/api/intro.html), [bridge README](https://github.com/vaddisrinivas/olivetin-mcp#readme).
