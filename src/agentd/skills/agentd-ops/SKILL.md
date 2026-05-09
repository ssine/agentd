---
name: agentd-ops
description: agentd maintenance and runtime troubleshooting. Use for agentd service management, config checks, context profiles, schedules, Feishu routing, and Codex app-server routing.
---

# agentd Operations

This skill is built into agentd and is injected into every agentd-managed Codex run.

## Workflow

1. For paths, use the injected runtime context first: `config_path`, `source_dir`, `state_dir`, `workspace`, `context_dir`, and `context_config_path`.
2. Before service changes, run a config check from `source_dir`:

```bash
"$AGENTD_CLI" --config "$AGENTD_CONFIG" config-check
```

3. For service status, logs, health checks, start, stop, or restart, use:

```bash
"$AGENTD_CLI" --config "$AGENTD_CONFIG" service ...
```

4. Prefer deferred restarts during active work:

```bash
"$AGENTD_CLI" --config "$AGENTD_CONFIG" service restart --defer 10
```

5. For machine-specific details, search the injected memory index first, then read only the relevant machine memory snippets.
