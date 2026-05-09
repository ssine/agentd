# agentd

Local control plane for routing Feishu messages into persistent Codex app-server threads.

## Prerequisites

- `uv` installed and available on `PATH`.
- A working Codex CLI/app-server command. The default config uses `codex`; verify it with `codex --version`, or pass `--codex-command /path/to/codex` during `agentd init`.
- Feishu bot `app_id` and `app_secret` for the Feishu listener. The local web gateway can run without Feishu credentials.
- Linux, WSL, or another environment that can run a long-lived Python process. `systemd --user` is optional; agentd falls back to a process backend for service management.

## Environment

From this repository root:

```bash
uv sync --dev
```

This creates `.venv/` and installs `lark-oapi` for the Feishu WebSocket listener.

## Feishu App Setup

Agentd uses a Feishu self-built app with bot capability. Users do not paste a
`tenant_access_token`, `user_access_token`, verification token, encrypt key, or
webhook URL into agentd. Configure these two values only:

- `app_id`: Feishu app ID from "Credentials & Basic Info", shaped like `cli_xxx`.
- `app_secret`: Feishu app secret from the same page, an opaque secret string.

Put them in `~/.agentd/agentd.toml`, or provide them as environment variables:

```bash
export AGENTD_FEISHU_APP_ID=cli_xxx
export AGENTD_FEISHU_APP_SECRET=xxx
```

Agentd exchanges those credentials for an internal tenant access token through
Feishu's `tenant_access_token/internal` API at runtime.

In Feishu Open Platform:

1. Open <https://open.feishu.cn/app> and create a Feishu "智能体应用" / self-built app.
2. Add the Bot capability.
3. Open `https://open.feishu.cn/app/{app_id}/event?tab=callback`, replacing `{app_id}` with your app ID such as `cli_xxx`.
4. In the callback/event subscription page, choose long connection/WebSocket event receiving.
5. Subscribe to `im.message.receive_v1` ("Receive Message v2.0").
6. Subscribe to `card.action.trigger` ("Card Action Interaction" / "卡片回传交互") so status-card buttons work.
7. Add the required permissions, then create a version and publish it.

Minimum permissions:

| Permission | Why agentd needs it |
| --- | --- |
| `im:message:send_as_bot` | Send status cards and final replies as the bot. |
| `im:message.p2p_msg:readonly` | Receive direct messages sent to the bot. |
| `im:message.group_at_msg:readonly` | Receive group messages that mention the bot. |
| `im:message:update` | Update the status card message in place. |

Optional permissions:

| Permission | When to enable |
| --- | --- |
| `im:message.group_msg` | Receive all group messages without requiring an @mention. This is broader than the default and may need admin approval. |
| `contact:user.base:readonly` | Improve sender display names if your tenant does not include names in message events. Agentd does not call contact APIs by default. |
| `im:resource` | Future media/file support. Current agentd only shows `[image]`, `[file]`, and `[audio]` placeholders. |

Some Feishu tenants show a broader `im:message` permission instead of, or in
addition to, granular message scopes. Prefer the granular scopes above when the
console offers them. If card updates fail with a no-permission error, add or
re-publish `im:message:update`.

## Setup

There are two supported setup paths:

- Agent-guided setup: paste the prompt below into Codex from this repository root and let the agent run `agentd init`, validate config, and guide service installation.
- Manual setup: run `agentd init` yourself, or create the same files by hand.

Agent-guided setup prompt:

```text
Help me set up agentd on this machine from the current cloned repository.

Goals:
- Install Python dependencies with uv.
- Verify `codex --version`, or ask me for the Codex command path and pass it through `agentd init --codex-command`.
- Run `uv run agentd --config ~/.agentd/agentd.toml init` to create ~/.agentd/agentd.toml and ~/agent-context if they do not exist.
- Initialize context.toml, schedules.toml, CONTEXT.md, memory/MEMORY.md, memory/projects/, and skills/ through that command.
- Ask me for my Feishu self-built app App ID and App Secret; explain that agentd needs app credentials, not a tenant_access_token or webhook secret.
- If Feishu credentials are missing, pause setup and tell me exactly where to create or find them:
  - Create a Feishu "智能体应用" / self-built app at https://open.feishu.cn/app if I do not have one.
  - In that app's "Credentials & Basic Info" page, copy App ID (`cli_xxx`) and App Secret.
  - Paste them back into this chat as `AGENTD_FEISHU_APP_ID=cli_xxx` and `AGENTD_FEISHU_APP_SECRET=...`.
  - After I paste them back, continue setup by writing them to ~/.agentd/agentd.toml or exporting them for this shell, then rerun config-check.
- Tell me to open https://open.feishu.cn/app/{app_id}/event?tab=callback after replacing `{app_id}` with my app ID, then add the `card.action.trigger` / "卡片回传交互" callback subscription.
- Confirm the Feishu app has Bot capability, long-connection events `im.message.receive_v1` and `card.action.trigger`, and the permissions documented in README.
- Keep secrets out of Git by default; use environment variables or tell me exactly where to edit app_id/app_secret.
- Set agentd.source_dir to this repository path and agentd.executable to .venv/bin/agentd.
- Run config-check and explain any missing values.
- If systemd --user is available, offer to install and start the service.

Constraints:
- Do not overwrite existing user files without showing me what would change.
- Keep my context repository user-controlled; do not put runtime state there.
- Use ~/.agentd/state for agentd runtime state.
```

## Config

Manual setup starts by creating local config files and filling secrets outside Git. `~/.agentd` is for agentd's own state; the context directory can be a separate user-managed git repo.

The recommended manual path is the init command:

```bash
uv sync --dev
codex --version
uv run agentd --config ~/.agentd/agentd.toml init
```

This creates `~/.agentd/agentd.toml` and a conservative `~/agent-context` skeleton without overwriting existing files. Use flags when you want custom paths:

```bash
uv run agentd --config ~/.agentd/agentd.toml init \
  --home-dir ~/.agentd \
  --context-dir ~/agent-context \
  --source-dir "$(pwd)"
```

If you prefer to do the same setup completely by hand:

```bash
mkdir -p ~/.agentd ~/agent-context
cp examples/agentd.example.toml ~/.agentd/agentd.toml
cp examples/context.example.toml ~/agent-context/context.toml
cp examples/schedules.example.toml ~/agent-context/schedules.toml
```

Create a minimal context skeleton:

```bash
mkdir -p ~/agent-context/memory/projects ~/agent-context/skills

cat > ~/agent-context/CONTEXT.md <<'EOF'
# CONTEXT.md

This is my private agent context repository.

- Runtime state belongs under ~/.agentd/state, not here.
- Long-term searchable notes live under memory/.
- Context-local skills live under skills/**/SKILL.md.
EOF

cat > ~/agent-context/memory/MEMORY.md <<'EOF'
# Memory Index

Search this directory with rg before loading deeper memory files.

- Project notes: memory/projects/
EOF

cat > ~/agent-context/.gitignore <<'EOF'
.env
.venv/
__pycache__/
EOF
```

Edit `~/.agentd/agentd.toml`:

- Set `agentd.source_dir` to this cloned repository path.
- Keep `agentd.executable = ".venv/bin/agentd"` unless you use a different install path.
- Keep `context.dir = "~/agent-context"` or point it at your own private context repository.

Required Feishu fields can also be supplied by environment variables:

```bash
export AGENTD_FEISHU_APP_ID=cli_xxx
export AGENTD_FEISHU_APP_SECRET=xxx
```

Then validate the setup:

```bash
uv sync --dev
uv run agentd --config ~/.agentd/agentd.toml config-check
uv run agentd --config ~/.agentd/agentd.toml simulate-message --chat-id local-p2p 'Reply exactly with: pong'
uv run agentd --config ~/.agentd/agentd.toml service doctor
```

If validation reports missing `app_id` or `app_secret`, create or open your
Feishu self-built app at <https://open.feishu.cn/app>, copy App ID and App
Secret from "Credentials & Basic Info", then put them in `~/.agentd/agentd.toml`
or export `AGENTD_FEISHU_APP_ID` and `AGENTD_FEISHU_APP_SECRET`. After that,
rerun `config-check` and continue from the same setup step.

After `config-check` is clean, run locally or install the service:

```bash
uv run agentd --config ~/.agentd/agentd.toml serve
uv run agentd --config ~/.agentd/agentd.toml service install --enable --now
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
- `schedules.toml`: lightweight scheduled jobs. A job can use `session = "schedule"` for an independent scheduled-task session, or `session = "main"` to post into the chat's main session and queue until that session is idle.

Agentd also injects its built-in `agentd-ops` skill into every managed Codex run.

The default context prompt files are configured in `context.toml`:

```toml
[context]
prompt_files = ["CONTEXT.md", "memory/MEMORY.md"]
prompt_file_max_bytes = 65536
```

Agentd has two child-thread creation modes:

- `spawn-child`: hand off the current task. The parent turn is interrupted and the child thread takes over.
- `spawn-branch`: start parallel work. The parent turn keeps running, and agentd posts a new top-level status card in the main Feishu chat for the branch. The branch thread is attached to that new card, not to the parent task card.

Child sessions cannot create nested child threads because Feishu threads do not support child threads. Start separate work from the main chat instead.

Child sessions can choose context explicitly:

```bash
printf %s "$child_task" | "$AGENTD_CLI" spawn-child \
  --cwd /path/to/work \
  --title "short title" \
  --profile personal \
  --skills bookkeeping,calendar
```

For parallel work, use:

```bash
printf %s "$child_task" | "$AGENTD_CLI" spawn-branch \
  --cwd /path/to/work \
  --title "short title"
```

When a main-chat turn is already running, users can also send `/branch <task>`
to start a parallel child task, or `/thread [title]` to create an empty Feishu
thread card in the main chat that starts Codex when the first message is posted
inside it.

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
