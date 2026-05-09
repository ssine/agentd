# agentd

Local control plane for routing Feishu messages into persistent Codex app-server threads.

## Environment

From this repository root:

```bash
uv sync --dev
```

This creates `.venv/` and installs `lark-oapi` for the Feishu WebSocket listener.

## Config

Create local config files and fill secrets outside Git. `~/.agentd` is for agentd's own state; the context directory can be a separate user-managed git repo:

```bash
mkdir -p ~/.agentd ~/agent-context
cp examples/agentd.example.toml ~/.agentd/agentd.toml
cp examples/context.example.toml ~/agent-context/context.toml
cp examples/schedules.example.toml ~/agent-context/schedules.toml
```

Required Feishu fields can also be supplied by environment variables:

```bash
export AGENTD_FEISHU_APP_ID=cli_xxx
export AGENTD_FEISHU_APP_SECRET=xxx
```

Important path defaults:

- `AGENTD_HOME` defaults to `~/.agentd`; the default config path is `~/.agentd/agentd.toml`.
- `agentd.source_dir` points at the cloned agentd source tree.
- `agentd.state_dir` stores agentd's own process and registry state, defaults to `AGENTD_HOME/state`, and is relative to `AGENTD_HOME` when not absolute.
- `agentd.workspace` is where Codex turns run and defaults to `context.dir`.
- `context.dir` points at user-maintained context, which can be a separate git repo.
- `context.config` and `context.schedules` are relative to `context.dir`.
- `codex.command` defaults to `codex`. Set it to an absolute path or wrapper command if needed.

## Commands

```bash
uv run agentd config-check
uv run agentd simulate-message --chat-id local-p2p 'Reply exactly with: pong'
uv run agentd serve # also starts the local web gateway when [web].enabled = true
uv run agentd web --host 127.0.0.1 --port 8765
```

Use a config outside this repository with `--config`:

```bash
uv run agentd --config ~/.agentd/agentd.toml config-check
```

Agentd-owned runtime state is stored under the configured `state_dir`:

- `agentd.sqlite`: Feishu chat/thread registry, durable run/event state, card projections, and Feishu outbox.
- `agentd.pid`: pid for the fallback process supervisor.
- `logs/`: per-turn Codex app-server JSON-RPC logs.
- `logs/agentd-service.log`: stdout/stderr for the fallback process supervisor.
- `captures/responses/`: raw Responses API captures when `codex.capture.enabled = true`; the current archive period stays as loose `.http` files and completed periods are compacted into `.tar.zst`.
- `captures/otel/`: Codex OpenTelemetry exports when `codex.otel.enabled = true`; OTLP JSON is stored as `.otlp.jsonl`, OTLP protobuf as `.otlp.pb`, and completed periods are compacted into `.tar.zst`.

Optional Codex Responses capture:

```toml
[codex.capture]
enabled = true
upstream_mode = "codex-default"
archive_period = "week" # day, week, or month
archive_format = "tar.zst"
zstd_level = 10
```

When enabled, `agentd` injects a temporary local model provider for the Codex app-server process and records final `POST /v1/responses` request/response exchanges under `state_dir`. Each live exchange is stored as one request `.http` and one response `.http` file under the current period directory, which defaults to the current ISO week. On startup and new captures, older period directories are archived as a single `tar.zst` while the SQLite index keeps the exchange metadata and archive member names. The proxy only handles the model provider endpoint; it does not change `chatgpt_base_url` or set global proxy environment variables.

Optional local Codex OpenTelemetry capture:

```toml
[codex.otel]
enabled = true
environment = "agentd"
protocol = "json"
log_user_prompt = false
logs = true
traces = true
metrics = true
```

When enabled, `agentd` starts a loopback OTLP/HTTP receiver for the Codex app-server process and injects Codex `[otel]` overrides pointing at that receiver. Exports are stored under `state_dir/captures/otel/` and indexed in `agentd.sqlite` table `otel_exports`. `protocol = "json"` writes OTLP JSON Lines files (`.otlp.jsonl`) for direct offline analysis; `protocol = "binary"` writes OTLP protobuf payloads (`.otlp.pb`) for compact archival and replay through OTLP tooling. `log_user_prompt` stays false by default, matching Codex's privacy-oriented default.

## Web Gateway

`agentd web` starts a local chat gateway that uses the same durable sessions and Codex runner without requiring Feishu credentials. The page lists recent runs, lets you send messages from a browser, and visualizes captured Responses API calls as a deduplicated request tree. Model/status/token metadata is shown on tree nodes when `codex.capture.enabled = true` and capture files are available.

When running `agentd serve`, the web gateway starts automatically by default:

```toml
[web]
enabled = true
host = "127.0.0.1"
port = 8765
```

Keep it on localhost unless an authenticated reverse proxy is in front of it.

## Context

Context is intentionally thin and user-controlled:

- `context.toml`: context profiles and allowed skill names.
- `CONTEXT.md`: context-level instructions injected into every Codex prompt by agentd.
- `memory/MEMORY.md`: memory index injected into every Codex prompt by agentd.
- `skills/**/SKILL.md`: context-local skills scanned by name from YAML frontmatter.
- `skills = ["*"]`: profile shorthand that injects every discovered skill.
- `memory/**`: deeper Markdown memory searched with `rg` by the agent when prior work, preferences, decisions, dates, people, or todos are relevant.
- `schedules.toml`: lightweight scheduled jobs.

Agentd also injects its built-in `agentd-ops` skill into every managed Codex run.

The default context prompt files are configured in `context.toml`:

```toml
[context]
prompt_files = ["CONTEXT.md", "memory/MEMORY.md"]
prompt_file_max_bytes = 65536
```

Child sessions can choose context explicitly:

```bash
printf %s "$child_task" | "$AGENTD_CLI" spawn-child \
  --cwd /path/to/work \
  --title "short title" \
  --profile personal \
  --skills bookkeeping,calendar
```

## Service Management

`agentd` can manage its own long-running service process. The preferred backend is `systemd --user`; if that is not available, the `process` backend starts `agentd serve` in the background and records a pid file under `state_dir`.

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
- `process`: used otherwise; writes `agentd.pid` and `logs/agentd-service.log` under `state_dir`.

To install the systemd user unit:

```bash
uv run agentd service install --enable --now
```

The generated unit uses the configured `agentd.executable` and `agentd.toml` path:

```text
ExecStart=/path/to/agentd/.venv/bin/agentd --config /home/me/.agentd/agentd.toml serve
Restart=always
RestartSec=3
```

Agentd persists run state, Codex events, Feishu card projections, and final-reply outbox records in SQLite.
After a restart it reconciles pending card/final-message updates and marks any leased in-flight Codex turn it
can no longer control as interrupted. Use `--defer` when you want to avoid interrupting an active turn:

```bash
uv run agentd service restart --defer
```

The CLI records a deferred restart request and the daemon applies it only after active runs, dirty cards, and
Feishu outbox sends are idle. A bare `--defer` uses a 10 second minimum delay; pass `--defer 30` to override it.

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run python -m unittest discover -s tests
uv run python -m compileall -q src
```
