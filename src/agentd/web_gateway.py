from __future__ import annotations

import json
import shlex
import threading
from dataclasses import asdict, is_dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .channels.web import WebChannelAdapter
from .config import AgentdConfig
from .registry import Registry
from .web_trace import build_responses_trace, exchange_detail, load_exchange


class WebGateway:
    def __init__(
        self,
        config: AgentdConfig,
        *,
        host: str = '127.0.0.1',
        port: int = 8765,
        daemon: Any | None = None,
    ) -> None:
        self.config = config
        self.host = host
        self.port = port
        if daemon is None:
            from .daemon import AgentDaemon

            daemon = AgentDaemon(config, dry_send=True)
        self.daemon = daemon
        self.registry = self.daemon.registry
        self.server: ThreadingHTTPServer | None = None

    def start_background(self) -> tuple[str, int]:
        self._ensure_server()
        assert self.server is not None
        thread = threading.Thread(
            target=self.server.serve_forever,
            name='agentd-web-gateway',
            daemon=True,
        )
        thread.start()
        host, port = self.server.server_address
        return str(host), int(port)

    def serve_forever(self) -> None:
        self._ensure_server()
        assert self.server is not None
        host, port = self.server.server_address
        print(f'agentd web gateway listening on http://{host}:{port}')
        self.server.serve_forever()

    def _ensure_server(self) -> None:
        if self.server is not None:
            return
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.config.log_dir.mkdir(parents=True, exist_ok=True)

        gateway = self

        class Handler(WebGatewayHandler):
            owner = gateway

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)


class WebGatewayHandler(BaseHTTPRequestHandler):
    owner: WebGateway
    protocol_version = 'HTTP/1.1'

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == '/':
            self._send_html(INDEX_HTML)
            return
        if parsed.path == '/api/state':
            query = parse_qs(parsed.query)
            run_id = parse_int(first(query.get('run_id')))
            self._send_json(build_state(self.owner.registry, selected_run_id=run_id))
            return
        if parsed.path == '/api/exchange':
            query = parse_qs(parsed.query)
            exchange_id = first(query.get('id'))
            row = self.owner.registry.get_model_http_exchange(exchange_id)
            if row is None:
                self._send_json({'ok': False, 'error': 'exchange not found'}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json({'ok': True, 'exchange': exchange_detail(load_exchange(row))})
            return
        self.send_error(HTTPStatus.NOT_FOUND, 'not found')

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == '/api/messages':
            try:
                payload = self._read_json()
                response = handle_web_message(self.owner.daemon, payload)
            except Exception as exc:
                self._send_json({'ok': False, 'error': str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        self.send_error(HTTPStatus.NOT_FOUND, 'not found')

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b'{}'
        value = json.loads(raw.decode('utf-8'))
        if not isinstance(value, dict):
            raise ValueError('request body must be a JSON object')
        return value

    def _send_json(self, value: Any, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode('utf-8')
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def handle_web_message(daemon: Any, payload: dict[str, Any]) -> dict[str, Any]:
    adapter = WebChannelAdapter()
    envelope = adapter.envelope_from_payload(payload)
    if not envelope.text:
        raise ValueError('text is required')

    registry = daemon.registry
    session_id = parse_int(payload.get('session_id'))
    session = registry.get_session(session_id) if session_id is not None else None
    chat_id = str(envelope.conversation_ref or (session.chat_id if session else 'web')).strip() or 'web'
    thread_id = session.thread_id if session and session.thread_id else envelope.thread_ref
    envelope = replace(envelope, conversation_ref=chat_id, thread_ref=thread_id or '')
    result = daemon.handle_control_command(adapter.submit_message(envelope))
    return {
        'ok': True,
        'result': result,
        'chat_id': chat_id,
    }


def build_state(registry: Registry, *, selected_run_id: int | None = None) -> dict[str, Any]:
    sessions = registry.list_sessions(limit=80)
    sessions_by_id = {session.id: session for session in sessions}
    runs = registry.list_runs(limit=120)
    selected = registry.get_run(selected_run_id) if selected_run_id is not None else (runs[0] if runs else None)
    selected_session = (
        sessions_by_id.get(selected.session_id) or registry.get_session(selected.session_id)
        if selected is not None
        else None
    )
    events = registry.list_run_events(selected.id) if selected is not None else []
    trace_rows = registry.list_model_http_exchanges(
        session_id=selected.session_id if selected is not None else None,
        codex_thread_id=selected.codex_thread_id if selected is not None else '',
        codex_turn_id=selected.turn_id if selected is not None else '',
        limit=250,
    )
    trace = build_responses_trace(trace_rows)
    run_dicts = []
    for run in runs:
        run_dict = record_to_dict(run)
        run_dict['codex_resume_command'] = codex_resume_command(run, sessions_by_id.get(run.session_id))
        run_dicts.append(run_dict)
    selected_run = record_to_dict(selected) if selected is not None else None
    if selected_run is not None and selected is not None:
        selected_run['codex_resume_command'] = codex_resume_command(selected, selected_session)
    return {
        'sessions': [record_to_dict(session) for session in sessions],
        'runs': run_dicts,
        'selected_run': selected_run,
        'selected_session': record_to_dict(selected_session) if selected_session is not None else None,
        'events': [
            {
                'id': event.id,
                'run_id': event.run_id,
                'event_type': event.event_type,
                'payload': event.payload,
                'created_at': event.created_at,
            }
            for event in events
        ],
        'trace': trace,
    }


def codex_resume_command(run: Any, session: Any | None) -> str:
    thread_id = str(getattr(run, 'codex_thread_id', '') or '')
    if not thread_id:
        return ''
    command = shlex.join(['codex', 'resume', thread_id])
    cwd = str(getattr(session, 'cwd', '') or '')
    return f'cd {shlex.quote(cwd)} && {command}' if cwd else command


def record_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return value
    return dict(value)


def parse_int(value: object) -> int | None:
    if value in (None, ''):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def first(values: list[str] | None) -> str:
    return values[0] if values else ''


def run_web_gateway(config: AgentdConfig, *, host: str, port: int) -> None:
    WebGateway(config, host=host, port=port).serve_forever()


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>agentd web</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #17202a;
      --muted: #667085;
      --accent: #246bfe;
      --ok: #168a4a;
      --bad: #b42318;
      --warn: #b54708;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #111316;
        --panel: #181b20;
        --line: #303641;
        --text: #e6e9ef;
        --muted: #a3acba;
        --accent: #72a7ff;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main {
      display: grid;
      grid-template-columns: 320px minmax(360px, 1fr) minmax(420px, 1.25fr);
      gap: 1px;
      min-height: 100vh;
      background: var(--line);
    }
    section {
      min-width: 0;
      background: var(--panel);
      display: flex;
      flex-direction: column;
      max-height: 100vh;
    }
    header {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .header-actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      min-width: 0;
    }
    h1, h2 { margin: 0; font-size: 14px; font-weight: 650; }
    .muted { color: var(--muted); }
    .scroll { overflow: auto; padding: 12px; }
    .run {
      width: 100%;
      border: 1px solid var(--line);
      background: transparent;
      color: inherit;
      text-align: left;
      padding: 0;
      margin-bottom: 8px;
      border-radius: 6px;
      overflow: hidden;
    }
    .run-select {
      width: 100%;
      border: 0;
      background: transparent;
      color: inherit;
      text-align: left;
      padding: 10px;
      cursor: pointer;
      font: inherit;
    }
    .run.active { border-color: var(--accent); box-shadow: inset 3px 0 0 var(--accent); }
    .run-title { font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .run-meta { margin-top: 4px; color: var(--muted); font-size: 12px; }
    .status-running { color: var(--accent); }
    .status-done { color: var(--ok); }
    .status-failed { color: var(--bad); }
    .status-stopped { color: var(--warn); }
    form {
      border-top: 1px solid var(--line);
      padding: 12px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
    }
    textarea {
      min-height: 72px;
      max-height: 180px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px;
      background: transparent;
      color: inherit;
      font: inherit;
    }
    input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      background: transparent;
      color: inherit;
      font: inherit;
    }
    button.primary {
      border: 0;
      border-radius: 6px;
      padding: 0 16px;
      background: var(--accent);
      color: #fff;
      font-weight: 650;
      cursor: pointer;
    }
    .message, .event, .trace-node, .exchange {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      margin-bottom: 8px;
      overflow-wrap: anywhere;
    }
    .message.user { border-left: 3px solid var(--accent); }
    .message.assistant { border-left: 3px solid var(--ok); }
    .message.system { border-left: 3px solid var(--muted); }
    .role { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
    .content { white-space: pre-wrap; }
    .trace-node { margin-left: min(calc(var(--depth) * 16px), 128px); }
    .trace-node.llm { border-left: 3px solid var(--ok); }
    .exchange { border-left: 3px solid var(--ok); }
    .exchange summary { cursor: pointer; list-style-position: inside; }
    .exchange-title { font-weight: 650; }
    .exchange-preview { margin-top: 8px; color: var(--muted); font-size: 12px; }
    .exchange-detail { margin-top: 10px; }
    .resume-command code {
      display: block;
      margin-top: 8px;
      white-space: pre-wrap;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }
    button.copy {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 4px 8px;
      background: transparent;
      color: inherit;
      cursor: pointer;
      font: inherit;
      font-size: 12px;
    }
    .json-tree {
      max-height: 460px;
      overflow: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      background: rgba(127, 127, 127, 0.06);
    }
    .json-node {
      margin: 2px 0;
    }
    .json-node summary {
      cursor: pointer;
      list-style-position: outside;
      overflow-wrap: anywhere;
    }
    .json-collapsed-value {
      display: inline-block;
      margin: 0;
      max-width: 100%;
      cursor: pointer;
      vertical-align: top;
    }
    .json-collapsed-value.is-open .json-node summary,
    .json-collapsed-value.is-open button {
      cursor: pointer;
    }
    .json-key[data-json-collapsed-toggle] {
      cursor: pointer;
    }
    .json-summary {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .json-actions {
      display: inline-flex;
      gap: 4px;
    }
    button.json-action {
      border: 1px solid var(--line);
      border-radius: 4px;
      padding: 1px 6px;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      font: inherit;
      font-size: 11px;
      line-height: 1.35;
    }
    button.json-action:hover {
      color: var(--text);
      border-color: var(--accent);
    }
    .json-children {
      margin-left: 14px;
      padding-left: 10px;
      border-left: 1px solid var(--line);
    }
    .json-row {
      padding: 2px 0;
      overflow-wrap: anywhere;
    }
    .json-key { color: var(--muted); }
    .json-string { color: #168a4a; }
    .json-number, .json-boolean { color: #b54708; }
    .json-null { color: var(--muted); }
    .json-punctuation { color: var(--muted); }
    .json-text-block {
      display: block;
      margin: 4px 0 8px;
      border: 1px solid rgba(36, 107, 254, 0.28);
      border-radius: 6px;
      background: rgba(36, 107, 254, 0.08);
      overflow: hidden;
    }
    .json-text-block pre {
      margin: 0;
      padding: 10px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .request-meta {
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px dashed var(--line);
      color: var(--muted);
      font-size: 12px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .empty { color: var(--muted); padding: 24px 4px; text-align: center; }
    @media (max-width: 1040px) {
      main { grid-template-columns: 280px 1fr; }
      section.trace { grid-column: 1 / -1; max-height: none; }
    }
    @media (max-width: 720px) {
      main { display: block; }
      section { max-height: none; min-height: 40vh; }
    }
  </style>
</head>
<body>
<main>
  <section>
    <header>
      <h1>agentd</h1>
      <span id="sync" class="muted"></span>
    </header>
    <div class="scroll">
      <div id="runs" style="margin-top:12px"></div>
    </div>
  </section>
  <section>
    <header>
      <h2>对话</h2>
      <div class="header-actions">
        <button id="resumeCopy" class="copy" type="button" hidden>复制 resume</button>
        <span id="runStatus" class="muted"></span>
      </div>
    </header>
    <div id="messages" class="scroll"></div>
    <form id="composer">
      <textarea id="text" placeholder="输入消息"></textarea>
      <button class="primary" type="submit">发送</button>
    </form>
  </section>
  <section class="trace">
    <header><h2>模型请求</h2><span id="traceStats" class="muted"></span></header>
    <div id="trace" class="scroll"></div>
  </section>
</main>
<script>
let selectedRunId = null;
let selectedSessionId = null;
let sending = false;
let openExchangeIds = new Set();
let openExchangeDetailSections = new Set();
let openJsonNodeKeys = new Set();
let renderedMessagesRunId = null;
let lastRunsSignature = '';
let lastMessagesSignature = '';
let lastTraceSignature = '';
const exchangeDetailCache = new Map();
const exchangeDetailScrollTops = new Map();

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}

function compact(value, limit = 1200) {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim();
  return text.length > limit ? text.slice(0, limit - 6) + ' ...(截断)' : text;
}

function compactNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value ?? '');
  if (Math.abs(number) < 10000) return String(number);
  const scaled = number / 1000;
  const text = scaled >= 100 ? scaled.toFixed(0) : scaled.toFixed(1);
  return `${text.replace(/\.0$/, '')}k`;
}

async function loadState() {
  const url = selectedRunId ? `/api/state?run_id=${encodeURIComponent(selectedRunId)}` : '/api/state';
  const response = await fetch(url, {cache: 'no-store'});
  const state = await response.json();
  renderState(state);
}

function renderState(state) {
  const selected = state.selected_run || null;
  selectedRunId = selected ? selected.id : selectedRunId;
  selectedSessionId = state.selected_session ? state.selected_session.id : null;
  document.getElementById('sync').textContent = new Date().toLocaleTimeString();
  const runs = state.runs || [];
  const events = state.events || [];
  const trace = state.trace || {};
  const runsSignature = signature([selectedRunId, runs]);
  if (runsSignature !== lastRunsSignature) {
    renderRuns(runs, selectedRunId);
    lastRunsSignature = runsSignature;
  }
  const messagesSignature = signature([selected, events]);
  if (messagesSignature !== lastMessagesSignature) {
    renderMessages(selected, events);
    lastMessagesSignature = messagesSignature;
  }
  const traceSignature = signature([selectedRunId, trace]);
  if (traceSignature !== lastTraceSignature) {
    renderTrace(trace);
    lastTraceSignature = traceSignature;
  }
}

function signature(value) {
  return JSON.stringify(value ?? null);
}

function renderRuns(runs, activeId) {
  const root = document.getElementById('runs');
  if (!runs.length) {
    root.innerHTML = '<div class="empty">暂无记录</div>';
    return;
  }
  root.innerHTML = runs.map(run => {
    const cls = run.id === activeId ? 'run active' : 'run';
    const phase = `status-${esc(run.status_phase || '')}`;
    const title = esc(run.display_title || run.subject || `run ${run.id}`);
    return `<div class="${cls}">
      <button class="run-select" type="button" data-run-id="${run.id}">
        <div class="run-title">${title}</div>
        <div class="run-meta"><span class="${phase}">${esc(run.status_phase)}</span> · #${run.id} · ${esc(run.status)}</div>
      </button>
    </div>`;
  }).join('');
  root.querySelectorAll('.run-select[data-run-id]').forEach(button => {
    button.addEventListener('click', () => {
      selectedRunId = Number(button.dataset.runId);
      loadState();
    });
  });
}

function renderMessages(run, events) {
  const root = document.getElementById('messages');
  document.getElementById('runStatus').textContent = run ? `#${run.id} ${run.status}` : '';
  renderResumeCopy(run);
  if (!run) {
    renderedMessagesRunId = null;
    root.innerHTML = '<div class="empty">暂无对话</div>';
    return;
  }
  const runChanged = renderedMessagesRunId !== run.id;
  const previousScrollTop = root.scrollTop;
  const shouldFollowBottom = runChanged || isScrolledToBottom(root);
  const blocks = [`<div class="message user"><div class="role">user</div><div class="content">${esc(run.prompt || '')}</div></div>`];
  for (const event of events) {
    const payload = event.payload || {};
    if (event.event_type === 'agent_message') {
      const phase = payload.phase || 'assistant';
      blocks.push(`<div class="message assistant"><div class="role">${esc(phase)}</div><div class="content">${esc(payload.text || '')}</div></div>`);
    } else if (event.event_type === 'tool_started') {
      blocks.push(`<div class="event"><div class="role">tool</div><div class="content">${esc(payload.tool || 'Tool')}${payload.detail ? ': ' + esc(payload.detail) : ''}</div></div>`);
    } else if (event.event_type === 'tool_completed') {
      blocks.push(`<div class="event"><div class="role">tool</div><div class="content">${payload.failed ? 'failed' : 'completed'} ${esc(payload.item_id || '')}</div></div>`);
    }
  }
  root.innerHTML = blocks.join('');
  wireCopyButtons(root);
  renderedMessagesRunId = run.id;
  root.scrollTop = shouldFollowBottom ? root.scrollHeight : previousScrollTop;
}

function renderResumeCopy(run) {
  const button = document.getElementById('resumeCopy');
  const command = run && run.codex_resume_command ? run.codex_resume_command : '';
  button.hidden = !command;
  button.dataset.copy = command;
  wireCopyButtons(button.parentElement);
}

function wireCopyButtons(root) {
  root.querySelectorAll('button[data-copy]').forEach(button => {
    if (button.dataset.copyWired === '1') return;
    button.dataset.copyWired = '1';
    button.addEventListener('click', async () => {
      const text = button.dataset.copy || '';
      try {
        await navigator.clipboard.writeText(text);
        const label = button.dataset.copyLabel || button.textContent || '复制';
        button.dataset.copyLabel = label;
        button.textContent = '已复制';
        setTimeout(() => { button.textContent = label; }, 1200);
      } catch {
        window.prompt('复制命令', text);
      }
    });
  });
}

function isScrolledToBottom(element) {
  return element.scrollHeight <= element.clientHeight
    || element.scrollHeight - element.scrollTop - element.clientHeight <= 12;
}

function renderTrace(trace) {
  const root = document.getElementById('trace');
  const exchanges = trace.exchanges || [];
  document.getElementById('traceStats').textContent = `${exchanges.length} requests`;
  captureExchangeScrollPositions(root);
  const previousScrollTop = root.scrollTop;
  if (!exchanges.length) {
    root.innerHTML = '<div class="empty">暂无请求捕获</div>';
    root.scrollTop = previousScrollTop;
    return;
  }
  root.innerHTML = exchanges.map(exchange => renderExchangeSummary(exchange, openExchangeIds.has(String(exchange.id || '')))).join('');
  root.scrollTop = previousScrollTop;
  root.querySelectorAll('details[data-exchange-id]').forEach(details => {
    details.addEventListener('toggle', () => {
      const id = details.dataset.exchangeId || '';
      if (details.open) {
        openExchangeIds.add(id);
        loadExchangeDetail(details);
      } else {
        openExchangeIds.delete(id);
      }
    });
    if (details.open) loadExchangeDetail(details);
  });
}

function captureExchangeScrollPositions(root) {
  root.querySelectorAll('[data-scroll-key]').forEach(element => {
    exchangeDetailScrollTops.set(element.dataset.scrollKey || '', element.scrollTop);
  });
}

function renderExchangeSummary(exchange, open) {
  const time = exchange.created_at ? new Date(exchange.created_at * 1000).toLocaleTimeString() : 'request';
  const title = `${time} · ${exchange.model || 'model'} · HTTP ${exchange.status_code || '?'}`;
  const preview = exchange.error || exchange.response_preview || exchange.request_path || '';
  return `<details class="exchange" data-exchange-id="${esc(exchange.id || '')}" ${open ? 'open' : ''}>
    <summary>
      <span class="exchange-title">${esc(title)}</span>
      ${requestMeta(exchange)}
      ${preview ? `<div class="exchange-preview">${esc(compact(preview, 260))}</div>` : ''}
    </summary>
    <div class="exchange-detail muted">加载中</div>
  </details>`;
}

async function loadExchangeDetail(details) {
  const id = details.dataset.exchangeId || '';
  const root = details.querySelector('.exchange-detail');
  if (exchangeDetailCache.has(id)) {
    root.classList.remove('muted');
    root.innerHTML = renderExchangeDetail(exchangeDetailCache.get(id), id);
    wireExchangeDetail(details);
    details.dataset.loaded = '1';
    return;
  }
  if (details.dataset.loaded === '1' || details.dataset.loading === '1') return;
  details.dataset.loading = '1';
  root.textContent = '加载中';
  try {
    const response = await fetch(`/api/exchange?id=${encodeURIComponent(id)}`, {cache: 'no-store'});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || 'load failed');
    exchangeDetailCache.set(id, result.exchange || {});
    root.classList.remove('muted');
    root.innerHTML = renderExchangeDetail(result.exchange || {}, id);
    wireExchangeDetail(details);
    details.dataset.loaded = '1';
  } catch (error) {
    root.classList.add('muted');
    root.textContent = error.message || String(error);
  } finally {
    details.dataset.loading = '';
  }
}

function renderExchangeDetail(exchange, exchangeId) {
  return `
    <div class="request-meta">
      ${exchange.upstream_url ? `<span>${esc(exchange.upstream_url)}</span>` : ''}
      ${exchange.codex_turn_id ? `<span>turn ${esc(exchange.codex_turn_id)}</span>` : ''}
    </div>
    <details data-detail-section="request" ${isExchangeDetailSectionOpen(exchangeId, 'request', true) ? 'open' : ''}>
      <summary>request</summary>
      ${renderJsonTree(exchange.request_json ?? {}, exchangeId, 'request')}
    </details>
    <details data-detail-section="response" ${isExchangeDetailSectionOpen(exchangeId, 'response', true) ? 'open' : ''}>
      <summary>response</summary>
      ${renderJsonTree(exchange.response_json ?? {}, exchangeId, 'response')}
    </details>
  `;
}

function wireExchangeDetail(details) {
  const exchangeId = details.dataset.exchangeId || '';
  details.querySelectorAll('.exchange-detail details[data-detail-section]').forEach(section => {
    section.addEventListener('toggle', () => {
      const key = exchangeDetailSectionKey(exchangeId, section.dataset.detailSection || '');
      if (section.open) {
        openExchangeDetailSections.add(key);
        openExchangeDetailSections.delete(`${key}:closed`);
      } else {
        openExchangeDetailSections.delete(key);
        openExchangeDetailSections.add(`${key}:closed`);
      }
    });
  });
  details.querySelectorAll('.json-node[data-json-node-key]').forEach(node => {
    node.addEventListener('toggle', () => {
      const key = node.dataset.jsonNodeKey || '';
      if (node.open) {
        openJsonNodeKeys.add(key);
        openJsonNodeKeys.delete(`${key}:closed`);
      } else {
        openJsonNodeKeys.delete(key);
        openJsonNodeKeys.add(`${key}:closed`);
      }
    });
  });
  details.querySelectorAll('button[data-json-action]').forEach(button => {
    button.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      const key = button.dataset.jsonNodeKey || '';
      const node = button.closest('.json-node');
      if (!node) return;
      setJsonSubtreeOpen(node, key, button.dataset.jsonAction === 'expand');
    });
  });
  wireCollapsedJsonToggle(details);
  details.querySelectorAll('[data-scroll-key]').forEach(element => {
    const key = element.dataset.scrollKey || '';
    if (exchangeDetailScrollTops.has(key)) {
      element.scrollTop = exchangeDetailScrollTops.get(key);
    }
    element.addEventListener('scroll', () => {
      exchangeDetailScrollTops.set(key, element.scrollTop);
    });
  });
}

function wireCollapsedJsonToggle(details) {
  if (details.dataset.jsonCollapsedToggleWired === '1') return;
  details.dataset.jsonCollapsedToggleWired = '1';
  details.addEventListener('click', event => {
    const target = event.target instanceof Element ? event.target : event.target?.parentElement;
    if (!target) return;
    const toggle = target.closest('[data-json-collapsed-toggle][data-json-node-key]');
    if (!toggle || !details.contains(toggle)) return;
    if (target.closest('button, a, input, textarea, select')) return;
    const key = toggle.dataset.jsonNodeKey || '';
    const summary = target.closest('.json-node summary');
    if (summary && toggle.contains(summary)) {
      const node = summary.closest('.json-node');
      if ((node?.dataset.jsonNodeKey || '') !== `${key}.$value`) return;
    } else if (target.closest('.json-node') && toggle.contains(target.closest('.json-node'))) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    toggleCollapsedJsonValue(details, details.dataset.exchangeId || '', key);
  });
  details.addEventListener('keydown', event => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const target = event.target instanceof Element ? event.target : event.target?.parentElement;
    if (!target) return;
    if (!target.matches('[data-json-collapsed-toggle][data-json-node-key]')) return;
    if (!details.contains(target)) return;
    event.preventDefault();
    event.stopPropagation();
    toggleCollapsedJsonValue(details, details.dataset.exchangeId || '', target.dataset.jsonNodeKey || '');
  });
}

function isExchangeDetailSectionOpen(exchangeId, section, defaultOpen = false) {
  const key = exchangeDetailSectionKey(exchangeId, section);
  return openExchangeDetailSections.has(key) || (defaultOpen && !openExchangeDetailSections.has(`${key}:closed`));
}

function exchangeDetailSectionKey(exchangeId, section) {
  return `${exchangeId}:${section}`;
}

function exchangeDetailScrollKey(exchangeId, section) {
  return `${exchangeId}:${section}:scroll`;
}

function renderJsonTree(value, exchangeId, label) {
  const scrollKey = exchangeDetailScrollKey(exchangeId, `${label}-json`);
  return `<div class="json-tree" data-scroll-key="${esc(scrollKey)}">
    ${renderJsonValue(value, jsonNodeKey(exchangeId, label, '$'), 0)}
  </div>`;
}

function renderJsonValue(value, nodeKey, depth) {
  if (Array.isArray(value)) return renderJsonArray(value, nodeKey, depth);
  if (value && typeof value === 'object') return renderJsonObject(value, nodeKey, depth);
  if (typeof value === 'string') return renderJsonString(value);
  if (typeof value === 'number') return `<span class="json-number">${esc(String(value))}</span>`;
  if (typeof value === 'boolean') return `<span class="json-boolean">${esc(String(value))}</span>`;
  if (value === null) return '<span class="json-null">null</span>';
  return `<span>${esc(JSON.stringify(value))}</span>`;
}

function renderJsonObject(value, nodeKey, depth) {
  const entries = Object.entries(value);
  const open = isJsonNodeOpen(nodeKey, depth);
  const meta = jsonObjectMeta(value);
  const count = `${entries.length} keys${meta ? ' · ' + meta : ''}`;
  return `<details class="json-node" data-json-node-key="${esc(nodeKey)}" ${open ? 'open' : ''}>
    <summary>${renderJsonNodeSummary('{}', count, nodeKey)}</summary>
    <div class="json-children">
      ${entries.map(([key, item]) => renderJsonMember(JSON.stringify(key), item, `${nodeKey}.${escapeJsonPathKey(key)}`, depth + 1)).join('')}
    </div>
  </details>`;
}

function renderJsonArray(value, nodeKey, depth) {
  const open = isJsonNodeOpen(nodeKey, depth);
  const meta = jsonArrayMeta(value, nodeKey);
  const count = `${value.length} items${meta ? ' · ' + meta : ''}`;
  return `<details class="json-node" data-json-node-key="${esc(nodeKey)}" ${open ? 'open' : ''}>
    <summary>${renderJsonNodeSummary('[]', count, nodeKey)}</summary>
    <div class="json-children">
      ${value.map((item, index) => renderJsonMember(`[${index}]`, item, `${nodeKey}[${index}]`, depth + 1)).join('')}
    </div>
  </details>`;
}

function renderJsonMember(label, value, nodeKey, depth) {
  if (isCollapsedJsonField(nodeKey)) {
    return `<div class="json-row">
      <span class="json-key" data-json-collapsed-toggle="1" data-json-node-key="${esc(nodeKey)}" role="button" tabindex="0">${esc(label)}</span><span class="json-punctuation">: </span>${renderCollapsedJsonValue(value, nodeKey, depth)}
    </div>`;
  }
  return `<div class="json-row">
    <span class="json-key">${esc(label)}</span><span class="json-punctuation">: </span>${renderJsonValue(value, nodeKey, depth)}
  </div>`;
}

function renderCollapsedJsonValue(value, nodeKey, depth) {
  const open = openJsonNodeKeys.has(nodeKey) && !openJsonNodeKeys.has(`${nodeKey}:closed`);
  const isComplex = Array.isArray(value) || (value && typeof value === 'object');
  const tag = open && isComplex ? 'div' : 'span';
  const content = open ? renderJsonValue(value, `${nodeKey}.$value`, depth + 1) : '<span class="json-punctuation">...</span>';
  return `<${tag} class="json-collapsed-value ${open ? 'is-open' : ''}" data-json-collapsed-toggle="1" data-json-node-key="${esc(nodeKey)}" role="button" tabindex="0">${content}</${tag}>`;
}

function renderJsonNodeSummary(shape, count, nodeKey) {
  return `<span class="json-summary">
    <span><span class="json-punctuation">${esc(shape)}</span> <span class="json-punctuation">${esc(count)}</span></span>
    ${isJsonActionHidden(nodeKey) ? '' : `<span class="json-actions">
      <button class="json-action" type="button" data-json-action="expand" data-json-node-key="${esc(nodeKey)}">递归展开</button>
      <button class="json-action" type="button" data-json-action="collapse" data-json-node-key="${esc(nodeKey)}">递归折叠</button>
    </span>`}
  </span>`;
}

function renderJsonString(value) {
  if (value.includes('\n')) {
    return `<div class="json-text-block"><pre>${esc(value)}</pre></div>`;
  }
  return `<span class="json-string">${esc(JSON.stringify(value))}</span>`;
}

const RESPONSE_COLLAPSED_FIELDS = new Set([
  'instructions',
  'tools',
  'tool_choice',
  'parallel_tool_calls',
  'reasoning',
  'text',
  'temperature',
  'top_p',
  'top_logprobs',
  'truncation',
  'store',
  'metadata',
  'user',
  'prompt_cache_key',
  'prompt_cache_retention',
  'service_tier',
  'background',
  'max_output_tokens',
  'max_tool_calls',
  'frequency_penalty',
  'presence_penalty',
  'safety_identifier',
  'moderation'
]);

const REQUEST_COLLAPSED_FIELDS = new Set([
  'instructions'
]);

function isCollapsedJsonField(nodeKey) {
  const responsePath = topLevelJsonPathAfterMarker(nodeKey, ':response:$.');
  if (responsePath && RESPONSE_COLLAPSED_FIELDS.has(responsePath)) return true;
  const requestPath = topLevelJsonPathAfterMarker(nodeKey, ':request:$.');
  return Boolean(requestPath && REQUEST_COLLAPSED_FIELDS.has(requestPath));
}

function topLevelJsonPathAfterMarker(nodeKey, marker) {
  const markerIndex = nodeKey.indexOf(marker);
  if (markerIndex < 0) return '';
  const path = nodeKey.slice(markerIndex + marker.length);
  if (!path || path.includes('.') || path.includes('[')) return '';
  return path;
}

function isJsonActionHidden(nodeKey) {
  return (nodeKey.includes(':response:$.') || nodeKey.includes(':request:$.')) && nodeKey.includes('.$value');
}

function jsonObjectMeta(value) {
  const parts = [];
  if (value.type) parts.push(String(value.type));
  if (value.role) parts.push(String(value.role));
  if (value.name) parts.push(String(value.name));
  if (value.status) parts.push(String(value.status));
  return parts.slice(0, 4).join(' · ');
}

function jsonArrayMeta(value, nodeKey) {
  const counts = new Map();
  for (const item of value) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
    const label = item.role || item.type || '';
    if (!label) continue;
    counts.set(label, (counts.get(label) || 0) + 1);
  }
  const entries = [...counts.entries()]
    .sort((left, right) => right[1] - left[1]);
  const visible = [];
  if (isRequestInputArray(nodeKey)) {
    for (const label of ['developer', 'system']) {
      if (counts.has(label)) visible.push([label, counts.get(label)]);
    }
  }
  for (const entry of entries) {
    if (visible.some(([label]) => label === entry[0])) continue;
    if (visible.length >= 6) break;
    visible.push(entry);
  }
  return visible
    .map(([label, count]) => `${label} ${count}`)
    .join(' · ');
}

function isRequestInputArray(nodeKey) {
  return String(nodeKey || '').endsWith(':request:$.input');
}

function isJsonNodeOpen(nodeKey, depth) {
  return openJsonNodeKeys.has(nodeKey) || (depth <= 1 && !openJsonNodeKeys.has(`${nodeKey}:closed`));
}

function toggleCollapsedJsonValue(exchangeDetails, exchangeId, nodeKey) {
  if (!nodeKey) return;
  const isOpen = openJsonNodeKeys.has(nodeKey) && !openJsonNodeKeys.has(`${nodeKey}:closed`);
  if (isOpen) {
    openJsonNodeKeys.delete(nodeKey);
    openJsonNodeKeys.add(`${nodeKey}:closed`);
  } else {
    openJsonNodeKeys.add(nodeKey);
    openJsonNodeKeys.delete(`${nodeKey}:closed`);
  }
  rerenderExchangeDetail(exchangeDetails, exchangeId);
}

function rerenderExchangeDetail(exchangeDetails, exchangeId) {
  const root = exchangeDetails.querySelector('.exchange-detail');
  const exchange = exchangeDetailCache.get(exchangeId);
  if (!root || !exchange) return;
  captureExchangeScrollPositions(exchangeDetails);
  root.innerHTML = renderExchangeDetail(exchange, exchangeId);
  wireExchangeDetail(exchangeDetails);
}

function setJsonSubtreeOpen(rootNode, rootKey, open) {
  const nodes = [rootNode, ...rootNode.querySelectorAll('.json-node[data-json-node-key]')];
  nodes.forEach(node => {
    const key = node.dataset.jsonNodeKey || '';
    node.open = open;
    if (open) {
      openJsonNodeKeys.add(key);
      openJsonNodeKeys.delete(`${key}:closed`);
    } else {
      openJsonNodeKeys.delete(key);
      openJsonNodeKeys.add(`${key}:closed`);
    }
  });
  if (rootKey) {
    if (open) {
      openJsonNodeKeys.add(rootKey);
      openJsonNodeKeys.delete(`${rootKey}:closed`);
    } else {
      openJsonNodeKeys.delete(rootKey);
      openJsonNodeKeys.add(`${rootKey}:closed`);
    }
  }
}

function jsonNodeKey(exchangeId, label, path) {
  return `${exchangeId}:${label}:${path}`;
}

function escapeJsonPathKey(key) {
  return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(key)
    ? key
    : `[${JSON.stringify(key)}]`;
}

function roleClass(role) {
  const value = String(role || '');
  if (value.includes('user')) return 'user';
  if (value.includes('assistant')) return 'assistant';
  if (value.includes('system') || value.includes('developer')) return 'system';
  return 'event';
}

function requestMeta(request) {
  const parts = [];
  if (request.model) parts.push(`model ${esc(request.model)}`);
  if (request.status_code) parts.push(`HTTP ${esc(request.status_code)}`);
  if (request.input_tokens != null) parts.push(`in ${esc(compactNumber(request.input_tokens))}`);
  if (request.output_tokens != null) parts.push(`out ${esc(compactNumber(request.output_tokens))}`);
  if (request.total_tokens != null) parts.push(`total ${esc(compactNumber(request.total_tokens))}`);
  if (request.storage_state) parts.push(esc(request.storage_state));
  parts.push(`#${esc(request.id || '')}`);
  return `<div class="request-meta">${parts.map(item => `<span>${item}</span>`).join('')}</div>`;
}

document.getElementById('composer').addEventListener('submit', async event => {
  event.preventDefault();
  if (sending) return;
  const text = document.getElementById('text').value.trim();
  if (!text) return;
  sending = true;
  try {
    const response = await fetch('/api/messages', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text, chat_id: 'web', session_id: selectedSessionId})
    });
    const result = await response.json();
    if (!result.ok) throw new Error(result.error || 'send failed');
    document.getElementById('text').value = '';
    await loadState();
  } catch (error) {
    alert(error.message || String(error));
  } finally {
    sending = false;
  }
});

loadState();
setInterval(loadState, 2500);
</script>
</body>
</html>
"""
