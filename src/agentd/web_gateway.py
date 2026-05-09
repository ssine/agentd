from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, is_dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import AgentdConfig
from .models import IncomingMessage
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
    text = str(payload.get('text') or '').strip()
    if not text:
        raise ValueError('text is required')

    registry = daemon.registry
    session_id = parse_int(payload.get('session_id'))
    session = registry.get_session(session_id) if session_id is not None else None
    chat_id = str(payload.get('chat_id') or (session.chat_id if session else 'web')).strip() or 'web'
    thread_id = session.thread_id if session and session.thread_id else str(payload.get('thread_id') or '')
    message = IncomingMessage(
        chat_id=chat_id,
        message_id=f'web-{time.time_ns()}',
        thread_id=thread_id or '',
        sender_open_id='web-user',
        sender_name='web',
        sender_type='user',
        text=text,
        chat_type='p2p',
    )
    result = daemon.handle_message(message)
    return {
        'ok': True,
        'result': result,
        'chat_id': chat_id,
    }


def build_state(registry: Registry, *, selected_run_id: int | None = None) -> dict[str, Any]:
    sessions = registry.list_sessions(limit=80)
    runs = registry.list_runs(limit=120)
    selected = registry.get_run(selected_run_id) if selected_run_id is not None else (runs[0] if runs else None)
    selected_session = registry.get_session(selected.session_id) if selected is not None else None
    events = registry.list_run_events(selected.id) if selected is not None else []
    trace_rows = registry.list_model_http_exchanges(
        session_id=selected.session_id if selected is not None else None,
        codex_thread_id=selected.codex_thread_id if selected is not None else '',
        codex_turn_id=selected.turn_id if selected is not None else '',
        limit=250,
    )
    trace = build_responses_trace(trace_rows)
    return {
        'sessions': [record_to_dict(session) for session in sessions],
        'runs': [record_to_dict(run) for run in runs],
        'selected_run': record_to_dict(selected) if selected is not None else None,
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
    h1, h2 { margin: 0; font-size: 14px; font-weight: 650; }
    .muted { color: var(--muted); }
    .scroll { overflow: auto; padding: 12px; }
    .run {
      width: 100%;
      border: 1px solid var(--line);
      background: transparent;
      color: inherit;
      text-align: left;
      padding: 10px;
      margin-bottom: 8px;
      border-radius: 6px;
      cursor: pointer;
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
    .json {
      max-height: 360px;
      overflow: auto;
      white-space: pre-wrap;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      background: rgba(127, 127, 127, 0.06);
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
      <input id="chatId" value="web" aria-label="chat id">
      <div id="runs" style="margin-top:12px"></div>
    </div>
  </section>
  <section>
    <header><h2>对话</h2><span id="runStatus" class="muted"></span></header>
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
const exchangeDetailCache = new Map();

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}

function compact(value, limit = 1200) {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim();
  return text.length > limit ? text.slice(0, limit - 6) + ' ...(截断)' : text;
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
  renderRuns(state.runs || [], selectedRunId);
  renderMessages(selected, state.events || []);
  renderTrace(state.trace || {});
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
    return `<button class="${cls}" data-run-id="${run.id}">
      <div class="run-title">${title}</div>
      <div class="run-meta"><span class="${phase}">${esc(run.status_phase)}</span> · #${run.id} · ${esc(run.status)}</div>
    </button>`;
  }).join('');
  root.querySelectorAll('button[data-run-id]').forEach(button => {
    button.addEventListener('click', () => {
      selectedRunId = Number(button.dataset.runId);
      loadState();
    });
  });
}

function renderMessages(run, events) {
  const root = document.getElementById('messages');
  document.getElementById('runStatus').textContent = run ? `#${run.id} ${run.status}` : '';
  if (!run) {
    root.innerHTML = '<div class="empty">暂无对话</div>';
    return;
  }
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
  root.scrollTop = root.scrollHeight;
}

function renderTrace(trace) {
  const root = document.getElementById('trace');
  const exchanges = trace.exchanges || [];
  document.getElementById('traceStats').textContent = `${exchanges.length} requests`;
  if (!exchanges.length) {
    root.innerHTML = '<div class="empty">暂无请求捕获</div>';
    return;
  }
  root.innerHTML = exchanges.map(exchange => renderExchangeSummary(exchange, openExchangeIds.has(String(exchange.id || '')))).join('');
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
    root.innerHTML = renderExchangeDetail(exchangeDetailCache.get(id));
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
    root.innerHTML = renderExchangeDetail(result.exchange || {});
    details.dataset.loaded = '1';
  } catch (error) {
    root.classList.add('muted');
    root.textContent = error.message || String(error);
  } finally {
    details.dataset.loading = '';
  }
}

function renderExchangeDetail(exchange) {
  const inputItems = exchange.request_input_items || [];
  const inputHtml = inputItems.length
    ? inputItems.map(item => `<div class="message ${roleClass(item.role)}">
        <div class="role">#${esc(item.index)} ${esc(item.role || 'item')}${item.type ? ' · ' + esc(item.type) : ''}</div>
        <div class="content">${esc(item.content || '(empty)')}</div>
      </div>`).join('')
    : '<div class="empty">无 request input</div>';
  const responseText = exchange.response_text || '';
  const rawRequest = exchange.request_json ? JSON.stringify(exchange.request_json, null, 2) : '';
  return `
    <div class="request-meta">
      <span>input items ${esc(exchange.request_input_count || 0)}</span>
      ${exchange.upstream_url ? `<span>${esc(exchange.upstream_url)}</span>` : ''}
      ${exchange.codex_turn_id ? `<span>turn ${esc(exchange.codex_turn_id)}</span>` : ''}
    </div>
    <div class="message assistant">
      <div class="role">response</div>
      <div class="content">${esc(responseText || '(empty)')}</div>
    </div>
    <details>
      <summary>request input</summary>
      ${inputHtml}
    </details>
    <details>
      <summary>raw request JSON</summary>
      <pre class="json">${esc(rawRequest || '{}')}</pre>
    </details>
  `;
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
  if (request.input_tokens != null) parts.push(`in ${esc(request.input_tokens)}`);
  if (request.output_tokens != null) parts.push(`out ${esc(request.output_tokens)}`);
  if (request.total_tokens != null) parts.push(`total ${esc(request.total_tokens)}`);
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
    const chatId = document.getElementById('chatId').value.trim() || 'web';
    const response = await fetch('/api/messages', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text, chat_id: chatId, session_id: selectedSessionId})
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
