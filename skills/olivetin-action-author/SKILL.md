---
name: olivetin-action-author
description: Author or review OliveTin YAML actions for use through olivetin-mcp, including stable IDs, safe arguments, execution policy, and isolated verification.
---

Read the existing configuration and [current argument documentation](https://docs.olivetin.app/args/types.html) before adding actions. Preserve existing actions, authentication, ACLs, entity definitions, and persistent data. Make changes in the deployment's owning configuration; the repository's example is a harmless demonstration.

Give each action a stable, unique `id`. The API exposes it as `bindingId`. Avoid the bridge's built-in tool names. Use a descriptive title and narrowly constrained argument types. `ascii_identifier` permits dots; `unicode` accepts shell-sensitive text. Follow [OliveTin's argument safety guidance](https://docs.olivetin.app/args/safety.html); prefer `exec` argument arrays for user-controlled text where supported, and never assume interpolated `shell` text is escaped automatically. The bridge normalizes defaults and booleans, rejects unknown arguments, and enforces declared choices; OliveTin remains authoritative for its specialized types.

Use `ro_` IDs or `[read-only]` titles only for actions proven to have no external side effects. These conventions bypass bridge approval; naming is an operator assertion, not a sandbox. Configuration access is privileged. Ordinary actions retain human approval. Add suitable timeouts, rate limits, and concurrency controls using OliveTin's documented configuration.

Validate a temporary configuration using `OliveTin -configdir <directory> -syntaxcheck`. Test harmless examples in an isolated OliveTin instance, refresh discovery, inspect schemas, execute representative valid arguments, and verify invalid arguments do not run. Do not test destructive commands against a live host merely to prove discovery works.

When changing the Python bridge, compare payloads with the [release-tagged protobuf](https://github.com/OliveTin/OliveTin/blob/3000.22.0/proto/olivetin/api/v1/olivetin.proto): `StartAction` uses `bindingId`; `StartActionAndWait` uses `actionId`. Both use a list of `{name, value}` strings. Calls are JSON POSTs under `/api/`. Never enable automatic retries for action-starting RPCs.

Use [the safe example](https://github.com/vaddisrinivas/olivetin-mcp/blob/main/examples/olivetin/config.yaml) as a starting point. Report configuration checks separately from actual execution checks.
