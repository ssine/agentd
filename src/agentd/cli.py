from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from .config import AgentdConfig, load_config
from .context import ContextResolver, split_skill_names
from .daemon import AgentDaemon
from .models import IncomingMessage
from .registry import Registry
from .title import TITLE_DISPLAY_WIDTH, normalize_title


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='agentd')
    parser.add_argument('--config', help='path to agentd TOML config')
    sub = parser.add_subparsers(dest='command', required=True)

    serve = sub.add_parser('serve', help='start Feishu websocket listener')
    serve.add_argument('--dry-send', action='store_true', help='print replies instead of sending to Feishu')

    simulate = sub.add_parser('simulate-message', help='route one local fake message through Codex')
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
    spawn_child.add_argument('--cwd', required=True, help='working directory for the child Codex session')
    spawn_child.add_argument('--title', default='', help='short label shown on the child task card')
    spawn_child.add_argument('--prompt', default='', help='prompt for the child Codex; stdin is used when omitted')
    spawn_child.add_argument('--profile', default='', help='context profile for the child Codex session')
    spawn_child.add_argument('--skills', default='', help='comma-separated skill names to add for the child session')
    spawn_child.add_argument('--parent-session-id', type=int, default=0)
    spawn_child.add_argument('--parent-status-message-id', default='')
    spawn_child.add_argument('--parent-source-message-id', default='')
    spawn_child.add_argument('--chat-id', default='')

    set_title = sub.add_parser('set-title', help='update the active Feishu status card title')
    set_title.add_argument('title', nargs='+', help=f'short title; capped to {TITLE_DISPLAY_WIDTH} display columns')
    set_title.add_argument('--session-id', type=int, default=0)

    sub.add_parser('config-check', help='check config without printing secrets')

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
    service_restart.add_argument('--defer', type=float, default=0, help='schedule restart after N seconds')

    service_logs = service_sub.add_parser('logs', parents=[service_common], help='show service logs')
    service_logs.add_argument('--tail', type=int, default=120)
    service_logs.add_argument('-f', '--follow', action='store_true')

    service_sub.add_parser('doctor', parents=[service_common], help='run service health checks')

    service_install = service_sub.add_parser('install', help='install the systemd user unit')
    service_install.add_argument('--enable', action='store_true', help='enable unit for user login')
    service_install.add_argument('--now', action='store_true', help='restart the unit after installing')

    args = parser.parse_args(argv)
    config = load_config(args.config)
    configure_logging(config.log_level)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)

    if args.command == 'config-check':
        return config_check(config)
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
        return spawn_child_request(config, args)
    if args.command == 'set-title':
        return set_title_request(config, args)
    parser.error('unreachable')


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
    print(f'codex_command={config.codex.command}')
    print(f'codex_capture_enabled={config.codex.capture.enabled}')
    print(f'codex_capture_dir={config.codex.capture.capture_dir}')
    print(f'codex_capture_archive_period={config.codex.capture.archive_period}')
    print(f'codex_capture_archive_format={config.codex.capture.archive_format}')
    print(f'codex_capture_zstd_level={config.codex.capture.zstd_level}')
    print(f'codex_capture_upstream_mode={config.codex.capture.upstream_mode}')
    if config.codex.capture.upstream_url:
        print(f'codex_capture_upstream_url={config.codex.capture.upstream_url}')
    if missing:
        print(f'missing={",".join(missing)}')
        return 2
    print('ok')
    return 0


def spawn_child_request(config: AgentdConfig, args: argparse.Namespace) -> int:
    parent_session_id = args.parent_session_id or int(os.environ.get('AGENTD_SESSION_ID') or 0)
    parent_status_message_id = args.parent_status_message_id or os.environ.get('AGENTD_STATUS_MESSAGE_ID') or ''
    parent_source_message_id = args.parent_source_message_id or os.environ.get('AGENTD_SOURCE_MESSAGE_ID') or ''
    chat_id = args.chat_id or os.environ.get('AGENTD_CHAT_ID') or ''
    if not parent_session_id or not parent_status_message_id or not chat_id:
        print(
            'missing parent context; run this from an agentd-managed Codex turn or pass '
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
    )
    print(f'spawn_request_id={request_id}')
    return 0


def set_title_request(config: AgentdConfig, args: argparse.Namespace) -> int:
    session_id = args.session_id or int(os.environ.get('AGENTD_SESSION_ID') or 0)
    if not session_id:
        print(
            'missing session context; run this from an agentd-managed Codex turn or pass --session-id', file=sys.stderr
        )
        return 2

    title = normalize_title(' '.join(args.title), fallback='任务')
    request_id = Registry(config.db_path).enqueue_title_request(session_id=session_id, title=title)
    print(f'title_request_id={request_id}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
