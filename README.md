# agentd

Local control plane for routing Feishu messages into persistent Codex app-server threads.

## Environment

From this repository root:

```bash
uv sync --dev
```

This creates `.venv/` and installs `lark-oapi` for the Feishu WebSocket listener.

## Config

Create local config files and fill secrets outside Git:

```bash
mkdir -p .agents/config
cp examples/agentd.example.toml .agents/config/agentd.toml
cp examples/context-profiles.example.toml .agents/config/context-profiles.toml
cp examples/schedules.example.toml .agents/config/schedules.toml
```

Required Feishu fields can also be supplied by environment variables:

```bash
export AGENTD_FEISHU_APP_ID=cli_xxx
export AGENTD_FEISHU_APP_SECRET=xxx
```

Important path defaults:

- `agentd.executable` is relative to this `agentd` project root and defaults to `.venv/bin/agentd`.
- `agentd.workspace` is where Codex turns run and defaults to this project root.
- `runtime_dir`, `context_profiles`, and `schedules` are relative to `agentd.workspace`.
- `codex.command` defaults to `bin/acodex`, a local wrapper around a shell `acodex` function. Set it to your own Codex command if needed.

## Commands

```bash
uv run agentd config-check
uv run agentd simulate-message --chat-id local-p2p 'Reply exactly with: pong'
uv run agentd serve
```

Use a config outside this repository with `--config`:

```bash
uv run agentd --config /path/to/workspace/.agents/config/agentd.toml config-check
```

Runtime state is stored under the configured `runtime_dir`:

- `agentd.sqlite`: Feishu chat/thread to Codex thread registry.
- `agentd.pid`: pid for the fallback process supervisor.
- `logs/`: per-turn Codex app-server JSON-RPC logs.
- `logs/agentd-service.log`: stdout/stderr for the fallback process supervisor.

## Context

Context is intentionally thin and workspace-controlled:

- `.agents/config/context-profiles.toml`: context profiles and allowed skill names.
- `skills/**/SKILL.md`: workspace-local skills scanned by name from YAML frontmatter.
- `memory/MEMORY.md` and `memory/daily/YYYY-MM-DD.md`: Markdown memory searched with `rg` by the agent when prior work, preferences, decisions, dates, people, or todos are relevant.
- `.agents/config/schedules.toml`: lightweight scheduled jobs.

Child sessions can choose context explicitly:

```bash
printf %s "$child_task" | "$AGENTD_CLI" spawn-child \
  --cwd /path/to/work \
  --title "short title" \
  --profile personal \
  --skills bookkeeping,calendar
```

## Service Management

`agentd` can manage its own long-running service process. The preferred backend is `systemd --user`; if that is not available, the `process` backend starts `agentd serve` in the background and records a pid file under `runtime_dir`.

```bash
uv run agentd service status
uv run agentd service start
uv run agentd service stop
uv run agentd service restart
uv run agentd service logs --tail 120
uv run agentd service doctor
```

Backend selection defaults to `auto`:

- `systemd`: used when `~/.config/systemd/user/agentd.service` exists and `systemd --user` is available.
- `process`: used otherwise; writes `agentd.pid` and `logs/agentd-service.log` under `runtime_dir`.

To install the systemd user unit:

```bash
uv run agentd service install --enable --now
```

The generated unit uses the configured `agentd.executable` and `agentd.toml` path:

```text
ExecStart=/path/to/agentd/.venv/bin/agentd --config /path/to/workspace/.agents/config/agentd.toml serve
Restart=always
RestartSec=3
```

Use `--defer` when a restart is requested from inside a Feishu-managed Codex turn:

```bash
uv run agentd service restart --defer 10
```

The CLI records a deferred restart request and the daemon applies it only after active runs finish.

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run python -m unittest discover -s tests
uv run python -m compileall -q src
```
