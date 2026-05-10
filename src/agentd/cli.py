from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from .codex_usage import CodexUsageError, format_codex_usage, read_codex_usage, snapshot_to_dict
from .config import (
    PROJECT_ROOT,
    AgentdConfig,
    default_home_dir,
    load_config,
)
from .context import ContextResolver, split_skill_names
from .daemon import AgentDaemon
from .models import IncomingMessage
from .registry import Registry
from .title import TITLE_DISPLAY_WIDTH, normalize_title

DEFAULT_DEFER_SECONDS = 10.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='agentd')
    parser.add_argument('--config', help='path to agentd TOML config')
    sub = parser.add_subparsers(dest='command', required=True)

    init = sub.add_parser('init', help='initialize local agentd config and context skeleton')
    init.add_argument('--home-dir', default='', help='agentd home directory; defaults to ~/.agentd')
    init.add_argument('--context-dir', default='', help='user context directory; defaults to ~/.agentd/context')
    init.add_argument('--source-dir', default='', help='agentd source tree; defaults to this repository')
    init.add_argument('--executable', default='.venv/bin/agentd', help='agentd executable path relative to source-dir')
    init.add_argument('--runner-kind', default='codex', choices=['codex', 'claude_code'], help='default agent runner')
    init.add_argument('--codex-command', default='codex', help='Codex command to put in agentd.toml')
    init.add_argument('--claude-command', default='aclaude', help='Claude Code command to put in agentd.toml')
    init.add_argument('--claude-model', default='sonnet', help='Claude Code model alias to put in agentd.toml')
    init.add_argument(
        '--create-feishu-app',
        action='store_true',
        help='create a Feishu PersonalAgent app through browser confirmation and save app_id/app_secret',
    )
    init.add_argument('--feishu-brand', choices=['feishu', 'lark'], default='feishu', help='platform brand for app setup')
    init.add_argument(
        '--feishu-registration-timeout',
        type=int,
        default=300,
        metavar='SECONDS',
        help='maximum time to wait for browser app creation confirmation',
    )
    init.add_argument('--overwrite', action='store_true', help='overwrite existing bootstrap files')

    serve = sub.add_parser('serve', help='start Feishu websocket listener')
    serve.add_argument('--dry-send', action='store_true', help='print replies instead of sending to Feishu')

    simulate = sub.add_parser('simulate-message', help='route one local fake message through the configured runner')
    simulate.add_argument('--chat-id', default='local-p2p')
    simulate.add_argument('--message-id', default='')
    simulate.add_argument('--thread-id', default='')
    simulate.add_argument('--sender-open-id', default='local-user')
    simulate.add_argument('--sender-name', default='local-user')
    simulate.add_argument('--send', action='store_true', help='send reply to Feishu; requires real message id')
    simulate.add_argument('text')

    spawn_child = sub.add_parser(
        'spawn-child', help='request the running daemon to hand off into a child Feishu thread'
    )
    add_spawn_request_arguments(spawn_child)

    spawn_branch = sub.add_parser(
        'spawn-branch', help='request the running daemon to start a parallel child Feishu thread'
    )
    add_spawn_request_arguments(spawn_branch)

    set_title = sub.add_parser('set-title', help='update the active Feishu status card title')
    set_title.add_argument('title', nargs='+', help=f'short title; capped to {TITLE_DISPLAY_WIDTH} display columns')
    set_title.add_argument('--session-id', type=int, default=0)

    sub.add_parser('config-check', help='check config without printing secrets')

    codex_usage = sub.add_parser('codex-usage', help='show Codex account usage and rate-limit windows')
    codex_usage.add_argument('--json', action='store_true', help='print machine-readable JSON')

    service = sub.add_parser('service', help='manage the agentd service process')
    service_sub = service.add_subparsers(dest='service_command', required=True)
    service_common = argparse.ArgumentParser(add_help=False)
    service_common.add_argument('--backend', choices=['auto', 'systemd', 'process'], default='auto')

    service_sub.add_parser('status', parents=[service_common], help='show service status')
    service_sub.add_parser('start', parents=[service_common], help='start agentd')

    service_stop = service_sub.add_parser('stop', parents=[service_common], help='stop agentd')
    service_stop.add_argument('--timeout', type=int, default=10)

    service_restart = service_sub.add_parser('restart', parents=[service_common], help='restart agentd')
    service_restart.add_argument('--timeout', type=int, default=10)
    service_restart.add_argument(
        '--defer',
        nargs='?',
        const=DEFAULT_DEFER_SECONDS,
        type=float,
        default=0,
        metavar='SECONDS',
        help=f'schedule restart after daemon is idle; optional minimum delay defaults to {DEFAULT_DEFER_SECONDS:g}s',
    )

    service_logs = service_sub.add_parser('logs', parents=[service_common], help='show service logs')
    service_logs.add_argument('--tail', type=int, default=120)
    service_logs.add_argument('-f', '--follow', action='store_true')

    service_sub.add_parser('doctor', parents=[service_common], help='run service health checks')

    service_install = service_sub.add_parser('install', help='install the systemd user unit')
    service_install.add_argument('--enable', action='store_true', help='enable unit for user login')
    service_install.add_argument('--now', action='store_true', help='restart the unit after installing')

    web = sub.add_parser('web', help='start local web chat gateway')
    web.add_argument('--host', default='127.0.0.1')
    web.add_argument('--port', type=int, default=8765)

    args = parser.parse_args(argv)
    if args.command == 'init':
        return init_request(args)

    config = load_config(args.config)
    configure_logging(config.log_level)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)

    if args.command == 'config-check':
        return config_check(config)
    if args.command == 'codex-usage':
        return codex_usage_request(config, args)
    if args.command == 'service':
        from .service import service_command

        return service_command(config, args)
    if args.command == 'serve':
        missing = missing_feishu_fields(config)
        if missing:
            print(f'missing Feishu config fields: {", ".join(missing)}', file=sys.stderr)
            return 2
        AgentDaemon(config, dry_send=args.dry_send).serve()
        return 0
    if args.command == 'web':
        from .web_gateway import run_web_gateway

        run_web_gateway(config, host=str(args.host), port=int(args.port))
        return 0
    if args.command == 'simulate-message':
        message_id = args.message_id or f'local-{int(time.time() * 1000)}'
        daemon = AgentDaemon(config, dry_send=not args.send)
        daemon.handle_message(
            IncomingMessage(
                chat_id=args.chat_id,
                message_id=message_id,
                thread_id=args.thread_id,
                sender_open_id=args.sender_open_id,
                sender_name=args.sender_name,
                sender_type='user',
                text=args.text,
                chat_type='p2p',
            )
        )
        return 0
    if args.command == 'spawn-child':
        return spawn_request(config, args, mode='handoff')
    if args.command == 'spawn-branch':
        return spawn_request(config, args, mode='branch')
    if args.command == 'set-title':
        return set_title_request(config, args)
    parser.error('unreachable')


def init_request(args: argparse.Namespace) -> int:
    from .bootstrap import BootstrapOptions, init_agentd, write_feishu_credentials

    home_dir = Path(args.home_dir).expanduser().resolve() if args.home_dir else default_home_dir()
    context_dir = Path(args.context_dir).expanduser().resolve() if args.context_dir else home_dir / 'context'
    source_dir = Path(args.source_dir).expanduser().resolve() if args.source_dir else PROJECT_ROOT
    config_path = Path(args.config).expanduser().resolve() if args.config else home_dir / 'agentd.toml'
    feishu_app_id = ''
    feishu_app_secret = ''

    if args.create_feishu_app:
        if config_path.exists() and not args.overwrite and _config_has_feishu_credentials(config_path):
            print(
                'existing Feishu credentials are already present; pass --overwrite to replace them',
                file=sys.stderr,
            )
            return 2
        try:
            feishu_app_id, feishu_app_secret = create_feishu_app(args)
        except Exception as exc:
            print(f'failed to create Feishu app: {exc}', file=sys.stderr)
            return 2

    result = init_agentd(
        BootstrapOptions(
            config_path=config_path,
            home_dir=home_dir,
            context_dir=context_dir,
            source_dir=source_dir,
            executable=str(args.executable or '.venv/bin/agentd'),
            runner_kind=str(args.runner_kind or 'codex'),
            codex_command=str(args.codex_command or 'codex'),
            claude_command=str(args.claude_command or 'aclaude'),
            claude_model=str(args.claude_model or 'sonnet'),
            feishu_app_id=feishu_app_id,
            feishu_app_secret=feishu_app_secret,
            overwrite=bool(args.overwrite),
        )
    )
    if args.create_feishu_app and config_path not in result.created:
        updated = write_feishu_credentials(
            config_path,
            app_id=feishu_app_id,
            app_secret=feishu_app_secret,
            overwrite=bool(args.overwrite),
        )
        if updated:
            print(f'updated Feishu credentials in {config_path}')
        else:
            print(
                'created Feishu app but did not overwrite existing credentials; '
                'pass --overwrite to replace them',
                file=sys.stderr,
            )
            return 2
    print(f'config_path={config_path}')
    print(f'context_dir={context_dir}')
    for path in result.created:
        print(f'created {path}')
    for path in result.skipped:
        print(f'skipped existing {path}')
    if args.create_feishu_app:
        print('created Feishu app and saved credentials')
        open_base = 'https://open.larksuite.com' if args.feishu_brand == 'lark' else 'https://open.feishu.cn'
        print(f'next: open {open_base}/app/{feishu_app_id}/event?tab=callback')
        print('next: subscribe im.message.receive_v1 and card.action.trigger, then publish the app version')
    else:
        print('next: fill Feishu credentials or export AGENTD_FEISHU_APP_ID/AGENTD_FEISHU_APP_SECRET')
    print(f'next: uv run agentd --config {config_path} config-check')
    return 0


def create_feishu_app(args: argparse.Namespace) -> tuple[str, str]:
    from .feishu_registration import FeishuAppRegistrationClient

    client = FeishuAppRegistrationClient(
        brand=str(args.feishu_brand or 'feishu'),
        timeout_seconds=int(args.feishu_registration_timeout or 300),
    )
    begin = client.begin()
    print('Open this URL to create the Feishu agent app:')
    print(begin.verification_url)
    print('Waiting for browser confirmation...')
    result = client.poll(begin)
    print(f'Feishu app created: app_id={result.app_id}')
    return result.app_id, result.app_secret


def _config_has_feishu_credentials(path: Path) -> bool:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
        import tomli as tomllib

    try:
        with path.open('rb') as fh:
            raw = tomllib.load(fh)
    except Exception:
        return True
    feishu = raw.get('feishu') if isinstance(raw.get('feishu'), dict) else {}
    return bool(feishu.get('app_id') or feishu.get('app_secret'))


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )


def missing_feishu_fields(config: AgentdConfig) -> list[str]:
    feishu = config.feishu
    missing: list[str] = []
    if not feishu.app_id:
        missing.append('app_id')
    if not feishu.app_secret:
        missing.append('app_secret')
    return missing


def config_check(config: AgentdConfig) -> int:
    missing = missing_feishu_fields(config)
    print(f'config_path={config.config_path}')
    print(f'home_dir={config.home_dir}')
    print(f'source_dir={config.source_dir}')
    print(f'executable={config.executable}')
    print(f'state_dir={config.state_dir}')
    print(f'workspace={config.workspace}')
    print(f'context_dir={config.context.context_dir}')
    print(f'context_config={config.context.path}')
    print(f'context_memory_dir={config.context.memory_dir}')
    print(f'context_default_profile={config.context.default_profile}')
    print(f'context_default_child_profile={config.context.default_child_profile}')
    print(f'context_skill_roots={",".join(str(path) for path in config.context.skill_roots)}')
    print(f'context_prompt_files={",".join(str(path) for path in config.context.prompt_files)}')
    print(f'context_prompt_file_max_bytes={config.context.prompt_file_max_bytes}')
    print(f'profiles_available={",".join(sorted(config.context.profiles))}')
    try:
        resolver = ContextResolver(config.context, config.workspace)
        print(f'skills_available={",".join(sorted(resolver.skills))}')
    except Exception as exc:
        print(f'skills_error={exc}')
    print(f'schedules={config.schedules.path}')
    print(f'schedules_jobs={len(config.schedules.jobs)}')
    print(f'feishu_app_id={"present" if config.feishu.app_id else "missing"}')
    print(f'feishu_app_secret={"present" if config.feishu.app_secret else "missing"}')
    print(f'web_enabled={config.web.enabled}')
    print(f'web_host={config.web.host}')
    print(f'web_port={config.web.port}')
    print(f'runner_kind={config.runner.kind}')
    print(f'codex_command={config.codex.command}')
    print(f'codex_capture_enabled={config.codex.capture.enabled}')
    print(f'codex_capture_dir={config.codex.capture.capture_dir}')
    print(f'codex_capture_archive_period={config.codex.capture.archive_period}')
    print(f'codex_capture_archive_format={config.codex.capture.archive_format}')
    print(f'codex_capture_zstd_level={config.codex.capture.zstd_level}')
    print(f'codex_capture_upstream_mode={config.codex.capture.upstream_mode}')
    if config.codex.capture.upstream_url:
        print(f'codex_capture_upstream_url={config.codex.capture.upstream_url}')
    print(f'codex_otel_enabled={config.codex.otel.enabled}')
    print(f'codex_otel_dir={config.codex.otel.capture_dir}')
    print(f'codex_otel_environment={config.codex.otel.environment}')
    print(f'codex_otel_protocol={config.codex.otel.protocol}')
    print(f'codex_otel_log_user_prompt={config.codex.otel.log_user_prompt}')
    print(f'codex_otel_logs={config.codex.otel.logs}')
    print(f'codex_otel_traces={config.codex.otel.traces}')
    print(f'codex_otel_metrics={config.codex.otel.metrics}')
    print(f'codex_otel_archive_period={config.codex.otel.archive_period}')
    print(f'codex_otel_archive_format={config.codex.otel.archive_format}')
    print(f'codex_otel_zstd_level={config.codex.otel.zstd_level}')
    print(f'claude_command={config.claude.command}')
    print(f'claude_model={config.claude.model}')
    print(f'claude_permission_mode={config.claude.permission_mode}')
    print(f'claude_use_login_shell={config.claude.use_login_shell}')
    if missing:
        print(f'missing={",".join(missing)}')
        return 2
    print('ok')
    return 0


def codex_usage_request(config: AgentdConfig, args: argparse.Namespace) -> int:
    try:
        snapshot = read_codex_usage(config)
    except CodexUsageError as exc:
        print(f'failed to read Codex usage: {exc}', file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(snapshot_to_dict(snapshot), ensure_ascii=False, sort_keys=True))
    else:
        print(format_codex_usage(snapshot))
    return 0


def add_spawn_request_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--cwd', required=True, help='working directory for the child agent session')
    parser.add_argument('--title', default='', help='short label shown on the child task card')
    parser.add_argument('--prompt', default='', help='prompt for the child agent; stdin is used when omitted')
    parser.add_argument('--profile', default='', help='context profile for the child agent session')
    parser.add_argument('--skills', default='', help='comma-separated skill names to add for the child session')
    parser.add_argument('--parent-session-id', type=int, default=0)
    parser.add_argument('--parent-status-message-id', default='')
    parser.add_argument('--parent-source-message-id', default='')
    parser.add_argument('--sender-open-id', default='')
    parser.add_argument('--chat-id', default='')


def spawn_request(config: AgentdConfig, args: argparse.Namespace, *, mode: str) -> int:
    session_kind = os.environ.get('AGENTD_SESSION_KIND') or ''
    if session_kind == 'child':
        print('nested child tasks are not supported; start separate work from the main Feishu chat', file=sys.stderr)
        return 2

    parent_session_id = args.parent_session_id or int(os.environ.get('AGENTD_SESSION_ID') or 0)
    parent_status_message_id = args.parent_status_message_id or os.environ.get('AGENTD_STATUS_MESSAGE_ID') or ''
    parent_source_message_id = args.parent_source_message_id or os.environ.get('AGENTD_SOURCE_MESSAGE_ID') or ''
    sender_open_id = args.sender_open_id or os.environ.get('AGENTD_SENDER_OPEN_ID') or ''
    chat_id = args.chat_id or os.environ.get('AGENTD_CHAT_ID') or ''
    if not parent_session_id or not parent_status_message_id or not chat_id:
        print(
            'missing parent context; run this from an agentd-managed agent turn or pass '
            '--parent-session-id, --parent-status-message-id, and --chat-id',
            file=sys.stderr,
        )
        return 2

    cwd = Path(args.cwd).expanduser()
    if not cwd.is_absolute():
        cwd = (Path.cwd() / cwd).resolve()
    if not cwd.is_dir():
        print(f'cwd is not a directory: {cwd}', file=sys.stderr)
        return 2

    prompt = args.prompt.strip() if args.prompt else sys.stdin.read().strip()
    if not prompt:
        print('missing child prompt; pass --prompt or pipe prompt on stdin', file=sys.stderr)
        return 2

    title = normalize_title(args.title.strip() or str(cwd), fallback='子任务')
    skills = split_skill_names(args.skills)
    request_id = Registry(config.db_path).enqueue_spawn_request(
        parent_session_id=parent_session_id,
        parent_status_message_id=parent_status_message_id,
        parent_source_message_id=parent_source_message_id,
        chat_id=chat_id,
        cwd=str(cwd),
        title=title,
        prompt=prompt,
        context_profile=str(args.profile or ''),
        skills=skills,
        sender_open_id=sender_open_id,
        mode=mode,
    )
    print(f'spawn_request_id={request_id}')
    return 0


def set_title_request(config: AgentdConfig, args: argparse.Namespace) -> int:
    session_id = args.session_id or int(os.environ.get('AGENTD_SESSION_ID') or 0)
    if not session_id:
        print(
            'missing session context; run this from an agentd-managed agent turn or pass --session-id', file=sys.stderr
        )
        return 2

    title = normalize_title(' '.join(args.title), fallback='任务')
    request_id = Registry(config.db_path).enqueue_title_request(session_id=session_id, title=title)
    print(f'title_request_id={request_id}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
