# agentd Architecture Notes

Status: merge-readiness draft.

This document records the current architecture discussion and the intended
direction for reorganizing agentd. It is not an implementation plan that must be
applied all at once.

## Decisions

The current direction is:

1. agentd is a multi-entry, multi-agent control plane.
   It should be able to evolve together with the agents it controls, instead of
   being only a Feishu-to-Codex bridge.
2. Feishu child threads and thread-based handoff are Feishu-specific channel
   capabilities.
   They should not become the universal session model in the core.
3. The Web gateway is a first-class external entry.
   It should eventually have complete functionality, not only debugging and
   observation features.
4. Future agents may be connected through multiple mechanisms:
   CLI runners, ACP, app-server style protocols like Codex, and direct API
   runners are all possible.
5. Database reset is acceptable.
   This gives us room to simplify the schema instead of carrying excessive
   compatibility constraints during the architecture cleanup.

## Current Shape

The repository is not large by file count, but some files have accumulated many
responsibilities.

The main pressure points are:

- `src/agentd/daemon.py`
  - Feishu listener startup.
  - Web gateway startup.
  - scheduler startup.
  - incoming message routing.
  - session resolution.
  - run lifecycle and active-run tracking.
  - agent runner invocation.
  - live steering and interrupt handling.
  - child task and branch handling.
  - title control.
  - status card projection and rendering.
  - Feishu outbox draining.
  - restart/deferred service coordination.
  - prompt and developer-instruction construction.
- `src/agentd/registry.py`
  - SQLite schema creation and migration.
  - sessions, runs, events, outbox, card projection, spawn requests, title
    requests, schedule dedupe, and trace queries.
- `src/agentd/web_gateway.py`
  - HTTP routing, API handlers, state projection, and embedded frontend in one
    file.
- `src/agentd/codex_app_server.py`
  - A mostly cohesive Codex runner implementation, but it is currently named
    and modeled as the only supported agent protocol.

Files like `capture_proxy.py`, `otel_capture.py`, and `web_trace.py` are large,
but their responsibilities are more cohesive. They are not the first refactor
target.

## Target Layers

## Validation Scope: Contracts and Fitness Tests

Before large file moves, the architecture needs executable boundary checks.

The first validation set is:

- Channels:
  - Feishu: full IM capability, including threads, child-thread handoff, card
    actions, and message updates.
  - Web: first-class local/API channel, not a fake Feishu chat.
  - WeCom: degraded IM capability. It can submit messages and receive text or
    markdown deliveries, but should not be assumed to support child threads,
    rich cards, or in-place message updates.
- Runners:
  - Codex app-server: high-capability runner with resume, live append,
    interrupt, title update, tool events, and structured events.
  - Claude Code CLI: lower-capability runner through the local `aclaude`/Claude
    Code command. It can run a turn and resume a saved Claude session when the
    CLI supports it, but live append, interrupt, and title update are not
    assumed. The runner defaults to the `sonnet` model alias because a smoke
    check showed the local `aclaude` wrapper defaults to a much more expensive
    Opus model. The model remains configurable under `[claude]`.

The fitness rule is:

- Adding a channel should require a new channel adapter, channel binding, and
  delivery renderer/dispatcher. It should not require changes to runner code.
- Adding a runner should require a new runner adapter and configuration. It
  should not require changes to Feishu, Web, or WeCom channel parsing.
- If a channel lacks a capability, the core degrades presentation. For example,
  WeCom should receive a text/markdown status instead of a Feishu-style
  interactive card.
- If a runner lacks a capability, control commands degrade explicitly. For
  example, Claude Code can return "live input unsupported" instead of pretending
  to steer an active turn.
- Entry adapters produce `ControlCommand` values before the existing daemon
  session/run flow is invoked. This is intentionally a thin facade in the
  refactor branch: it proves the direction without forcing a full daemon split
  in the same step.

This refactor introduces these contracts without resetting the database yet.
Existing field names such as `codex_thread_id` are compatibility aliases for a
generic runner session reference until the persistence cleanup phase can remove
or isolate Codex-specific trace paths.

The refactor adds two compatibility tables:

- `channel_bindings`
  - Durably binds a session to a channel, conversation reference, thread
    reference, and root message reference.
  - This lets Web and WeCom conversations be selected by durable binding instead
    of by source-message prefixes such as `web-*`.
- `deliveries`
  - Records channel-neutral outgoing deliveries before channel-specific
    dispatch.
  - Feishu deliveries still fan out to the existing `feishu_outbox` for retry
    and reliability.
  - Web and WeCom deliveries can be marked delivered locally without creating a
    fake Feishu outbox item.

The refactor has a concrete `DeliveryDispatcher`:

- The core/daemon creates channel-neutral `DeliveryRequest` values.
- `DeliveryDispatcher` records them in `deliveries`.
- Feishu dispatch drains the existing `feishu_outbox`, updates card projections,
  and marks the matching generic delivery sent or failed.
- Non-Feishu deliveries can be completed without Feishu-specific state.
- `AgentDaemon` still keeps thin compatibility methods such as
  `_drain_feishu_outbox` while callers are migrated.

It also starts the runner persistence migration:

- `sessions.runner_kind` and `sessions.runner_session_ref` are the generic
  runner session fields.
- `runs.runner_kind`, `runs.runner_session_ref`, and `runs.runner_turn_ref` are
  the generic run-level runner fields.
- `codex_thread_id` and `turn_id` remain populated as compatibility aliases
  while Codex-specific capture and Web trace code still use those names.

The delivery seam is:

- `ChannelBinding` maps `RunRecord` + `AgentSession` + durable
  `channel_bindings` data to a channel destination. Legacy inference remains
  only as a migration fallback.
- `DeliveryRequest` is channel-neutral. Feishu deliveries still use the existing
  durable `feishu_outbox` table for reliability, while Web deliveries update
  local state and WeCom deliveries intentionally degrade to text/markdown.
- This keeps the current reliable Feishu path intact while making it explicit
  that Web and WeCom should not be forced into Feishu card semantics.

The projection/rendering seam is now executable too:

- `run_projection.py`
  - Owns `RunView` and `RunIteration`.
  - Projects `RunRecord + RunEvent[]` into channel-neutral status state.
  - Keeps model output and tool lifecycle projection out of `AgentDaemon`.
- `status_rendering.py`
  - Renders a `RunView` into channel-neutral status text and Feishu-compatible
    status cards.
  - Keeps Feishu card structure, markdown escaping, action button values, and
    text truncation policy out of daemon lifecycle code.
- `AgentDaemon` still exposes thin compatibility methods such as
  `_format_status_text` and `_build_status_card` while callers and tests are
  migrated.

The core command facade has started:

- `agent_core.py`
  - Owns `AgentCore`.
  - Interprets `ControlCommand` values.
  - Handles submit-message routing, active-run live input, interrupt/status
    commands, branch/thread commands, and Feishu card action semantics.
  - Keeps the channel adapters from calling daemon internals directly.
- `active_run.py`
  - Owns the runtime-only `ActiveRun` object used to track an in-process run.
- `AgentDaemon`
  - Remains the process owner for now: threads, scheduler, spawn watcher,
    Feishu listener, Web gateway, and service lifecycle.
  - Keeps thin compatibility methods such as `_handle_submit_message`,
    `_handle_live_input`, and `handle_card_action` while callers are migrated.

The runner turn lifecycle has also started moving out:

- `run_executor.py`
  - Owns `RunExecutor`.
  - Executes a runner turn.
  - Consumes normalized runner events.
  - Updates runner session/turn refs, run state, tool events, final answers,
    retryable errors, terminal status, and active-run cleanup.
- `AgentDaemon`
  - Still starts the worker/status threads for compatibility with existing
    tests and process ownership.
  - Keeps thin compatibility methods such as `_run_turn_worker`,
    `_status_ticker`, and `_handle_codex_event` that delegate to `RunExecutor`.

The runner context boundary is now explicit:

- `run_context.py`
  - Owns external-message prompt construction.
  - Owns child-task and scheduled-task prompt construction.
  - Owns injected developer instructions and runner environment variables.
  - Produces the runner-facing context tuple:
    `ResolvedContext + extra_env + developer_instructions`.
- `AgentCore`
  - Uses `RunContextBuilder` to construct the user prompt when a channel submits
    a message.
- `RunExecutor`
  - Uses `RunnerContextBuilder` immediately before calling the selected runner.
  - Does not know whether the prompt came from Feishu, Web, WeCom, scheduler,
    or a child task.
- `AgentDaemon`
  - Keeps thin compatibility methods such as `_build_prompt`,
    `_developer_instructions`, and `_codex_extra_env` while older tests and
    call sites are migrated.

This is an important seam for adding Claude Code: the runner receives the same
structured context envelope as Codex, while runner-specific capabilities and
protocol details stay behind the runner adapter.

The Claude Code runner writes injected developer instructions to a prompt file
and passes it with `--append-system-prompt-file`. This avoids sending a large
multi-line context block through `zsh -lic` shell quoting.

Feishu child-thread coordination is also separated:

- `spawn_coordinator.py`
  - Owns claimed spawn requests.
  - Creates Feishu child/thread intro cards.
  - Binds child sessions to Feishu thread ids.
  - Applies handoff/branch/thread mode effects to the parent run.
  - Starts the child runner turn through the daemon-owned worker hooks.
- `AgentCore`
  - Only interprets live `/branch` and `/thread` commands and enqueues a spawn
    request.
- `AgentDaemon`
  - Still owns the watcher loop and exposes compatibility methods such as
    `_handle_spawn_request` and `_create_child_thread`.

This keeps Feishu's child-thread mechanics out of submit-message handling and
runner execution. It also makes the future Web/WeCom degradation clearer:
channels that cannot create child threads can reject or reinterpret related-run
requests without touching runner adapters.

### 1. Entry Adapters

Entry adapters receive external input and translate it into core control-plane
commands.

Likely adapters:

- `entries/feishu`
  - Feishu WebSocket listener.
  - Feishu message parsing.
  - Feishu card action parsing.
  - Feishu-specific thread and child-thread operations.
- `entries/web`
  - HTTP/API server.
  - Web session creation and message submission.
  - Web-native state and control APIs.
- `entries/cli`
  - Local command-line operations.
  - Spawn/branch/title/control commands.
  - Service commands that need to talk to the running daemon.
- `entries/scheduler`
  - Time-based command creation.
- `entries/service`
  - Local process supervision commands.
  - Graceful restart coordination.

Entry adapters should not directly know how a runner turn is started. They should
only create commands such as:

- `SubmitMessage`
- `AppendInput`
- `InterruptRun`
- `StartBranch`
- `StartHandoff`
- `SetRunTitle`
- `RequestServiceRestart`

### 2. Control Plane Core

The core owns the durable state machine.

Responsibilities:

- Resolve or create conversations/sessions.
- Create runs.
- Enforce one active run per session where appropriate.
- Dispatch runs to an agent runner.
- Consume agent events.
- Maintain run state, event log, and projections.
- Decide when a final reply is ready.
- Decide which deliveries should be queued.
- Apply user control commands such as append, interrupt, branch, and title.

The core should not import Feishu SDK code or Codex JSON-RPC details.

Useful core concepts:

- `Conversation`
  - A logical user-facing context.
  - Not necessarily a Feishu chat or thread.
- `Session`
  - A bound agent execution context inside a conversation.
  - Has an `agent_kind`.
- `Run`
  - One submitted unit of work.
- `RunEvent`
  - Append-only event stream used for status projection and audit.
- `Delivery`
  - A channel-bound outgoing message or update.
- `ControlCommand`
  - Durable command accepted by the core.

### 3. Agent Runner Layer

The runner layer abstracts different agent protocols.

The interface should describe capabilities, not Codex-specific implementation
details.

Sketch:

```python
class AgentRunner:
    kind: str
    capabilities: AgentCapabilities

    def start_turn(
        self,
        request: AgentTurnRequest,
        event_sink: AgentEventSink,
    ) -> AgentTurnHandle:
        ...


class AgentTurnHandle:
    session_ref: str
    turn_ref: str
    control: AgentRunControl


class AgentRunControl:
    def append_input(self, text: str) -> ControlResult: ...
    def interrupt(self) -> ControlResult: ...
    def set_title(self, title: str) -> ControlResult: ...
```

Agent event types should be normalized enough for the core:

- `session_ready`
- `turn_started`
- `message_delta`
- `message_completed`
- `tool_started`
- `tool_completed`
- `plan_updated`
- `title_updated`
- `final_ready`
- `turn_completed`
- `turn_failed`
- `turn_interrupted`

Codex-specific raw event parsing belongs inside the Codex runner.

Initial runner implementations can be:

- `runners/codex_app_server`
  - Wraps the current `CodexAppServer`.
  - Owns Codex JSON-RPC details.
  - Owns Codex-specific capture and OTEL setup for now.
- `runners/cli`
  - Starts a process and captures stdout/stderr.
  - May have limited control capabilities.
- `runners/acp`
  - Talks to agents through ACP.
- `runners/api`
  - Calls a direct API.

Each runner should declare capability flags:

- supports resume.
- supports live append/steer.
- supports interrupt.
- supports title update.
- supports tool events.
- supports final streaming.
- supports structured run events.

The core should degrade behavior based on these capabilities.

### 4. Projection and Rendering

The core should project run events into channel-independent views.

Flow:

```text
Run + RunEvent[] -> RunView -> ChannelRenderer -> Delivery
```

Examples:

- `RunStatusProjector`
  - Builds a compact `RunView`.
  - Contains no Feishu JSON.
- `FeishuStatusRenderer`
  - Converts `RunView` into Feishu interactive card JSON.
  - Owns Feishu button values.
- `WebStatusRenderer`
  - Converts `RunView` into Web API JSON.
- `TextRenderer`
  - Produces CLI or plain-text status.

This makes Feishu card controls a channel feature, while preserving the same
underlying run state for Web and CLI.

This refactor currently implements this as:

- `run_projection.py` for `Run + RunEvent[] -> RunView`.
- `status_rendering.py` for `RunView -> text/card`.
- `DeliveryDispatcher` for `DeliveryRequest -> channel-specific durable
  dispatch`.

### 5. Persistence

Given that database reset is acceptable, the next schema can be simpler and more
generic.

Possible durable tables:

- `conversations`
  - channel, external ids, metadata.
- `sessions`
  - conversation id, agent kind, workspace/cwd, context profile, skills,
    agent session ref.
- `runs`
  - session id, state, title, prompt, agent kind, agent turn ref, timestamps.
- `run_events`
  - run id, event type, payload json, created_at.
- `control_commands`
  - command type, payload json, state, attempts, error.
- `deliveries`
  - channel, destination, kind, payload json, state, dedupe key.
- `channel_bindings`
  - maps Feishu/Web/CLI-specific message ids to conversations/runs.
- `captures` or runner-specific trace tables.

The current `Registry` can first be split into smaller repositories without
changing tables. A schema reset can happen later when the interfaces are clear.

## Boundary: External Entries and IM Control

### Feishu

Feishu should become an adapter around the core.

Feishu-specific things:

- chat id.
- message id.
- sender open id.
- thread id.
- reply-in-thread behavior.
- card action payloads.
- child thread creation.
- message update limitations and permissions.

Core-level equivalents:

- conversation id.
- user message.
- run id.
- control command.
- delivery request.

Feishu child task and branch behavior should be represented in the core as
control commands, but actual thread creation remains in the Feishu adapter.

### Web

Web is a formal entry and should not be forced to emulate Feishu.

It needs its own:

- session creation.
- message submission.
- run list.
- run detail.
- control actions.
- delivery/state update mechanism.
- authentication or local trust boundary, eventually.

The Web gateway can reuse the core state and runner abstraction, but the UI/API
should not depend on Feishu status card concepts.

### CLI

The CLI should be a local control client.

It can:

- submit control commands to the running daemon.
- inspect status.
- create runs.
- request branch/handoff when running under an agentd-managed session.
- manage service lifecycle.

It should avoid directly constructing Feishu-specific state except where the
user explicitly invokes Feishu-specific actions.

### Service Control

Service management has two distinct parts:

- Supervisor operations: start, stop, restart, logs, doctor.
- Core-aware graceful operations: restart after active runs are idle, startup
  notice, deferred command processing.

The first part can remain a local service module. The second part should use the
same durable command path as other control-plane operations.

## Migration Plan

### Phase 1: Mechanical Extraction

Low-risk file splits with behavior preserved:

- Extract prompt construction from `daemon.py`.
- Extract developer-instruction construction from `daemon.py`.
- Extract run-event projection from `daemon.py`.
- Extract Feishu status-card rendering from `daemon.py`.
- Extract Feishu outbox dispatch from `daemon.py`.
- Extract branch and child-thread command parsing from `daemon.py`.

This phase should mostly move code and add focused tests.

This refactor has completed most of these extractions behind thin
compatibility methods:

- prompt and runner context construction: `run_context.py`.
- run-event projection: `run_projection.py`.
- status text/card rendering: `status_rendering.py`.
- Feishu/generic delivery dispatch: `delivery_dispatcher.py`.
- submit/live/card command handling: `agent_core.py`.
- runner execution and normalized event handling: `run_executor.py`.
- Feishu child-thread spawn coordination: `spawn_coordinator.py`.

### Phase 2: Core Command Facade

Introduce a small core facade:

- `submit_message(...)`
- `append_input(...)`
- `interrupt_run(...)`
- `set_title(...)`
- `start_branch(...)`
- `start_handoff(...)`

Feishu, Web, CLI, and scheduler should call this facade instead of reaching
into daemon internals.

The current `AgentDaemon` can remain as the process owner while its internals
move behind the facade.

This refactor implements the first slice of this as `AgentCore`. Runner worker
execution and event consumption now live in `RunExecutor`, while thread
ownership and service lifecycle remain in `AgentDaemon`.

### Phase 3: Runner Interface

Define generic runner types and adapt the current Codex implementation behind
them.

Keep Codex behavior unchanged initially:

- app-server startup.
- resume behavior.
- steer/interrupt/title methods.
- capture proxy and OTEL setup.
- Codex event parsing.

This refactor uses Claude Code as the second runner to validate the interface.
Further runners should wait until this two-runner shape survives real operation.

### Phase 4: Persistence Cleanup

Split `Registry` by responsibility:

- schema/migrations.
- session repository.
- run repository.
- event repository.
- command repository.
- delivery repository.
- trace repository.

After that, decide whether to reset the DB schema to the generic model.

### Phase 5: First-Class Web

Once the core facade exists, evolve Web from a local gateway into a full entry:

- Web-native conversations/sessions.
- complete run controls.
- streaming or polling status.
- trace and event inspection.
- eventually auth and multi-user boundaries if needed.

### Phase 6: Additional Agents

Add new runners after the runner interface is real:

- CLI runner first if the goal is easy integration.
- ACP runner if protocol compatibility is the priority.
- API runner when a direct API-backed agent is needed.

Avoid designing for too many imagined capabilities before another concrete
runner or channel forces the next interface change.

## Merge Readiness Checklist

Before merging this refactor into the original repository:

- Run `uv run ruff check`.
- Run `uv run python -m unittest discover -s tests`.
- Run a SQLite migration rehearsal against a copy of the current production
  database.
- Verify the default `codex` runner still handles a Feishu main run and a
  Feishu child/branch request.
- Verify Web can submit a message through `/api/messages` and record Web
  deliveries without writing Feishu outbox items.
- Verify `claude_code` can run one small non-interactive turn through `aclaude`
  with `[runner].kind = "claude_code"` and `[claude].model = "sonnet"`.
- Review the generated `agentd init` template so new installs expose both
  runner families.

## Deferred Work

These are intentional follow-ups, not merge blockers for this refactor:

- Split `Registry` into repositories after the schema shape stabilizes.
- Make Web trace and resume UI runner-neutral. Today it still exposes Codex
  trace/capture details because capture remains Codex-specific.
- Add a real WeCom entry service if WeCom becomes a production channel. The
  current WeCom work is a degraded adapter and delivery contract.
- Decide whether to reset the DB schema or keep compatibility aliases longer.
- Move Feishu child-thread creation behind channel capability methods once a
  non-Feishu channel supports related-run presentation.

## What Not To Refactor First

Do not start with:

- replacing SQLite.
- fully redesigning the Web frontend.
- turning every module into a plugin.
- making Feishu thread behavior universal.
- abstracting capture/OTEL before the runner boundary exists.
- preserving old DB schema at all costs.

The first payoff should come from reducing `daemon.py` coupling and making
entry adapters talk to a small core interface.

## Main Risks

- Over-abstracting before there is a second runner.
- Accidentally making Feishu concepts part of the core model.
- Making Web second-class by keeping it as a fake Feishu chat.
- Splitting files without creating clearer ownership.
- Losing operational reliability around durable runs, final replies, and
  deferred restart behavior.
- Creating a runner interface that cannot represent lower-capability agents
  such as simple CLI processes.

## Open Questions

These still need product/design decisions:

1. Should Web conversations share history with Feishu conversations, or should
   they be separate unless explicitly linked?
2. Should one conversation be allowed to contain sessions for multiple agent
   kinds at the same time?
3. What is the minimum capability set required for an agent to be usable in
   agentd?
4. Should branch/handoff be core concepts, or should the core only know about
   "start related run" and let channels decide how to present it?
5. Should capture and trace data be runner-owned, or should agentd define a
   generic trace model?
