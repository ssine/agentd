from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AgentdConfig

UNIT_NAME = 'agentd.service'
SERVICE_LOG_NAME = 'agentd-service.log'
DEFERRED_SERVICE_REQUEST_NAME = 'agentd-service-request.json'
STARTUP_NOTICE_NAME = 'agentd-startup-notice.json'
RESTARTING_NOTICE_TEXT = 'agentd 即将重启，重启期间消息收发会短暂中断。'
STARTED_NOTICE_TEXT = 'agentd 已启动成功，正在监听 Feishu 消息。'


@dataclass(frozen=True)
class Check:
    level: str
    name: str
    detail: str


def service_command(config: AgentdConfig, args: Any) -> int:
    command = str(args.service_command)
    backend = str(getattr(args, 'backend', 'auto'))

    if command == 'install':
        return install_systemd_unit(config, enable=bool(args.enable), start_now=bool(args.now))
    if command == 'status':
        return print_status(config, backend)
    if command == 'start':
        return start_service(config, backend)
    if command == 'stop':
        return stop_service(config, backend, timeout_seconds=int(args.timeout))
    if command == 'restart':
        defer_seconds = float(args.defer)
        if defer_seconds > 0:
            return defer_service_command(
                config,
                backend,
                'restart',
                defer_seconds,
                timeout_seconds=int(args.timeout),
            )
        notify_chat_id = service_notice_chat_id()
        prepare_restart_notice(config, notify_chat_id)
        selected = select_backend(backend)
        if selected == 'systemd':
            return subprocess.run(['systemctl', '--user', 'restart', UNIT_NAME], check=False).returncode
        stop_service(config, backend, timeout_seconds=int(args.timeout))
        return start_service(config, backend)
    if command == 'logs':
        return print_logs(config, backend, tail=int(args.tail), follow=bool(args.follow))
    if command == 'doctor':
        return doctor(config, backend)

    print(f'unknown service command: {command}', file=sys.stderr)
    return 2


def install_systemd_unit(config: AgentdConfig, *, enable: bool, start_now: bool) -> int:
    unit_dir = Path.home() / '.config/systemd/user'
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / UNIT_NAME
    unit_path.write_text(systemd_unit(config), encoding='utf-8')
    print(f'installed {unit_path}')

    if not systemd_available():
        print('systemd --user is not available; unit was written but cannot be loaded here', file=sys.stderr)
        return 1

    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=False)
    if enable:
        subprocess.run(['systemctl', '--user', 'enable', UNIT_NAME], check=False)
    if start_now:
        return subprocess.run(['systemctl', '--user', 'restart', UNIT_NAME], check=False).returncode
    return 0


def systemd_unit(config: AgentdConfig) -> str:
    agentd = agentd_executable(config)
    return '\n'.join(
        [
            '[Unit]',
            'Description=agentd Feishu to Codex bridge',
            'After=network-online.target',
            '',
            '[Service]',
            'Type=simple',
            f'WorkingDirectory={config.workspace}',
            f'Environment=AGENTD_CONFIG={config.config_path}',
            f'ExecStart={agentd} --config {config.config_path} serve',
            'Restart=always',
            'RestartSec=3',
            'TimeoutStopSec=10',
            '',
            '[Install]',
            'WantedBy=default.target',
            '',
        ]
    )


def print_status(config: AgentdConfig, backend: str) -> int:
    selected = select_backend(backend)
    print(f'backend={selected}')
    print(f'workspace={config.workspace}')
    print(f'config={config.config_path}')
    print(f'state_dir={_state_dir(config)}')
    print(f'agentd={agentd_executable(config)}')

    if selected == 'systemd':
        return systemd_status()
    return process_status(config)


def start_service(config: AgentdConfig, backend: str) -> int:
    selected = select_backend(backend)
    if selected == 'systemd':
        return subprocess.run(['systemctl', '--user', 'start', UNIT_NAME], check=False).returncode
    return start_process(config)


def stop_service(config: AgentdConfig, backend: str, *, timeout_seconds: int = 10) -> int:
    selected = select_backend(backend)
    if selected == 'systemd':
        return subprocess.run(['systemctl', '--user', 'stop', UNIT_NAME], check=False).returncode
    return stop_process(config, timeout_seconds=timeout_seconds)


def print_logs(config: AgentdConfig, backend: str, *, tail: int, follow: bool) -> int:
    selected = select_backend(backend)
    if selected == 'systemd':
        cmd = ['journalctl', '--user', '-u', UNIT_NAME, '-n', str(tail), '--no-pager']
        if follow:
            cmd.append('-f')
        return subprocess.call(cmd)

    log_path = service_log_path(config)
    if not log_path.exists():
        print(f'log file does not exist: {log_path}', file=sys.stderr)
        return 1
    print_tail(log_path, tail)
    if follow:
        follow_file(log_path)
    return 0


def doctor(config: AgentdConfig, backend: str) -> int:
    checks = collect_checks(config, backend)
    for check in checks:
        print(f'[{check.level}] {check.name}: {check.detail}')
    return 1 if any(check.level == 'fail' for check in checks) else 0


def collect_checks(config: AgentdConfig, backend: str) -> list[Check]:
    checks: list[Check] = []
    checks.append(check_path('workspace', config.workspace, must_be_dir=True))
    checks.append(check_path('config', config.config_path, must_be_file=True))
    checks.append(check_path('state_dir', _state_dir(config), must_be_dir=True, create=True))
    checks.append(check_path('log_dir', config.log_dir, must_be_dir=True, create=True))
    checks.append(check_executable('agentd executable', agentd_executable(config)))
    checks.append(check_command('codex command', config.codex.command))

    if config.feishu.app_id and config.feishu.app_secret:
        checks.append(Check('ok', 'Feishu credentials', 'present'))
    else:
        missing = []
        if not config.feishu.app_id:
            missing.append('app_id')
        if not config.feishu.app_secret:
            missing.append('app_secret')
        checks.append(Check('fail', 'Feishu credentials', 'missing ' + ', '.join(missing)))

    try:
        from .registry import Registry

        Registry(config.db_path).connect().close()
        checks.append(Check('ok', 'registry sqlite', f'read/write ok: {config.db_path}'))
    except Exception as exc:
        checks.append(Check('fail', 'registry sqlite', str(exc)))

    if systemd_available():
        checks.append(Check('ok', 'systemd --user', 'available'))
    else:
        checks.append(Check('warn', 'systemd --user', 'not available; process backend can be used'))

    selected = select_backend(backend)
    running = systemd_is_active() if selected == 'systemd' else pid_running(read_pid(pid_path(config)))
    checks.append(Check('ok' if running else 'warn', 'service running', 'yes' if running else 'no'))
    checks.extend(recent_log_checks(config))
    return checks


def check_path(
    name: str, path: Path, *, must_be_dir: bool = False, must_be_file: bool = False, create: bool = False
) -> Check:
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        if must_be_dir and not path.is_dir():
            return Check('fail', name, f'not a directory: {path}')
        if must_be_file and not path.is_file():
            return Check('fail', name, f'not a file: {path}')
        probe = path if path.is_file() else path / '.agentd-write-check'
        if path.is_file():
            readable = os.access(path, os.R_OK)
            return Check('ok' if readable else 'fail', name, str(path))
        probe.write_text('ok\n', encoding='utf-8')
        probe.unlink(missing_ok=True)
        return Check('ok', name, str(path))
    except Exception as exc:
        return Check('fail', name, f'{path}: {exc}')


def check_executable(name: str, executable: Path) -> Check:
    if executable.is_file() and os.access(executable, os.X_OK):
        return Check('ok', name, str(executable))
    return Check('fail', name, f'not executable: {executable}')


def check_command(name: str, command: str) -> Check:
    parts = shlex.split(command)
    if not parts:
        return Check('fail', name, 'empty')
    executable = parts[0]
    if '/' in executable:
        return check_executable(name, Path(executable))
    resolved = shutil.which(executable)
    if resolved:
        return Check('ok', name, command)
    return Check('fail', name, f'executable not found: {executable}')


def recent_log_checks(config: AgentdConfig) -> list[Check]:
    checks: list[Check] = []
    paths = [service_log_path(config), *sorted(config.log_dir.glob('codex-app-server-*.log'))[-3:]]
    for path in paths:
        if not path.exists():
            continue
        text = '\n'.join(tail_lines(path, 200))
        if 'Traceback ' in text or ' ERROR ' in text:
            checks.append(Check('warn', f'recent log errors in {path.name}', 'check service logs'))
    if not checks:
        checks.append(Check('ok', 'recent logs', 'no traceback/error markers in recent tails'))
    return checks


def select_backend(backend: str) -> str:
    if backend in {'systemd', 'process'}:
        return backend
    if systemd_unit_path().exists() and systemd_available():
        return 'systemd'
    return 'process'


def systemd_status() -> int:
    if not systemd_available():
        print('systemd --user is not available', file=sys.stderr)
        return 1

    active = subprocess.run(
        ['systemctl', '--user', 'is-active', UNIT_NAME], text=True, capture_output=True, check=False
    )
    enabled = subprocess.run(
        ['systemctl', '--user', 'is-enabled', UNIT_NAME], text=True, capture_output=True, check=False
    )
    print(f'unit={UNIT_NAME}')
    print(f'unit_path={systemd_unit_path()}')
    print(f'active={active.stdout.strip() or active.stderr.strip() or "unknown"}')
    print(f'enabled={enabled.stdout.strip() or enabled.stderr.strip() or "unknown"}')
    return 0 if active.returncode == 0 else 1


def systemd_available() -> bool:
    if shutil.which('systemctl') is None:
        return False
    try:
        result = subprocess.run(
            ['systemctl', '--user', 'show-environment'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def systemd_is_active() -> bool:
    if not systemd_available():
        return False
    result = subprocess.run(
        ['systemctl', '--user', 'is-active', '--quiet', UNIT_NAME],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def systemd_unit_path() -> Path:
    return Path.home() / '.config/systemd/user' / UNIT_NAME


def process_status(config: AgentdConfig) -> int:
    path = pid_path(config)
    pid = read_pid(path)
    running = pid_running(pid)
    print(f'pid_file={path}')
    print(f'pid={pid or ""}')
    print(f'running={"yes" if running else "no"}')
    print(f'log={service_log_path(config)}')
    return 0 if running else 1


def start_process(config: AgentdConfig) -> int:
    _state_dir(config).mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    path = pid_path(config)
    pid = read_pid(path)
    if pid_running(pid):
        print(f'agentd is already running: pid={pid}')
        return 0
    if pid:
        path.unlink(missing_ok=True)

    log_path = service_log_path(config)
    log = log_path.open('a', encoding='utf-8')
    env = os.environ.copy()
    env['AGENTD_CONFIG'] = str(config.config_path)
    env.setdefault('PYTHONUNBUFFERED', '1')
    proc = subprocess.Popen(
        [str(agentd_executable(config)), '--config', str(config.config_path), 'serve'],
        cwd=config.workspace,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    path.write_text(f'{proc.pid}\n', encoding='utf-8')
    print(f'started agentd pid={proc.pid}')
    print(f'log={log_path}')
    return 0


def stop_process(config: AgentdConfig, *, timeout_seconds: int) -> int:
    path = pid_path(config)
    pid = read_pid(path)
    if not pid:
        print('agentd is not running: no pid file')
        return 0
    if not pid_running(pid):
        path.unlink(missing_ok=True)
        print(f'agentd is not running: removed stale pid {pid}')
        return 0

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not pid_running(pid):
            path.unlink(missing_ok=True)
            print(f'stopped agentd pid={pid}')
            return 0
        time.sleep(0.2)

    os.kill(pid, signal.SIGKILL)
    while pid_running(pid):
        time.sleep(0.1)
    path.unlink(missing_ok=True)
    print(f'killed agentd pid={pid}')
    return 0


def defer_service_command(
    config: AgentdConfig,
    backend: str,
    command: str,
    defer_seconds: float,
    *,
    timeout_seconds: int = 10,
) -> int:
    selected = select_backend(backend)
    notify_chat_id = service_notice_chat_id()
    if command == 'restart' and service_running(config, selected):
        if not daemon_can_consume_deferred_service_command(config, selected):
            launch_idle_service_command(
                config,
                selected,
                command,
                not_before=time.time() + defer_seconds,
                timeout_seconds=timeout_seconds,
            )
            print(
                f'scheduled agentd service restart after active runs finish via external idle watcher, '
                f'not before {defer_seconds:g}s'
            )
            return 0
        write_deferred_service_command(
            config,
            {
                'command': command,
                'backend': selected,
                'not_before': time.time() + defer_seconds,
                'timeout_seconds': timeout_seconds,
                'created_at': time.time(),
                'notify_chat_id': notify_chat_id,
            },
        )
        print(f'scheduled agentd service restart after active runs finish, not before {defer_seconds:g}s')
        return 0

    launch_service_command(config, selected, command, delay_seconds=defer_seconds, timeout_seconds=timeout_seconds)
    print(f'scheduled agentd service {command} in {defer_seconds:g}s')
    return 0


def service_running(config: AgentdConfig, backend: str) -> bool:
    if backend == 'systemd':
        return systemd_is_active()
    return pid_running(read_pid(pid_path(config)))


def daemon_can_consume_deferred_service_command(config: AgentdConfig, backend: str) -> bool:
    pid = service_main_pid(config, backend)
    started_at = pid_started_at(pid)
    if started_at <= 0:
        return False
    code_mtime = max(Path(__file__).stat().st_mtime, (Path(__file__).parent / 'daemon.py').stat().st_mtime)
    return started_at >= code_mtime


def service_main_pid(config: AgentdConfig, backend: str) -> int:
    if backend == 'systemd':
        if not systemd_available():
            return 0
        result = subprocess.run(
            ['systemctl', '--user', 'show', UNIT_NAME, '--property=MainPID', '--value'],
            text=True,
            capture_output=True,
            check=False,
        )
        try:
            return int(result.stdout.strip())
        except ValueError:
            return 0
    return read_pid(pid_path(config))


def pid_started_at(pid: int) -> float:
    if pid <= 0:
        return 0
    try:
        stat = (Path('/proc') / str(pid) / 'stat').read_text(encoding='utf-8')
        uptime = float(Path('/proc/uptime').read_text(encoding='utf-8').split()[0])
    except (OSError, ValueError, IndexError):
        return 0
    end = stat.rfind(')')
    if end == -1:
        return 0
    parts = stat[end + 2 :].split()
    if len(parts) <= 19:
        return 0
    try:
        start_ticks = int(parts[19])
        ticks_per_second = int(os.sysconf('SC_CLK_TCK'))
    except (OSError, ValueError):
        return 0
    return time.time() - uptime + (start_ticks / ticks_per_second)


def deferred_service_request_path(config: AgentdConfig) -> Path:
    return _state_dir(config) / DEFERRED_SERVICE_REQUEST_NAME


def write_deferred_service_command(config: AgentdConfig, payload: dict[str, Any]) -> None:
    _state_dir(config).mkdir(parents=True, exist_ok=True)
    path = deferred_service_request_path(config)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False) + '\n', encoding='utf-8')
    tmp.replace(path)


def read_deferred_service_command(config: AgentdConfig) -> dict[str, Any] | None:
    path = deferred_service_request_path(config)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def clear_deferred_service_command(config: AgentdConfig) -> None:
    deferred_service_request_path(config).unlink(missing_ok=True)


def startup_notice_path(config: AgentdConfig) -> Path:
    return _state_dir(config) / STARTUP_NOTICE_NAME


def write_startup_notice(config: AgentdConfig, payload: dict[str, Any]) -> None:
    _state_dir(config).mkdir(parents=True, exist_ok=True)
    path = startup_notice_path(config)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False) + '\n', encoding='utf-8')
    tmp.replace(path)


def read_startup_notice(config: AgentdConfig) -> dict[str, Any] | None:
    path = startup_notice_path(config)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def clear_startup_notice(config: AgentdConfig) -> None:
    startup_notice_path(config).unlink(missing_ok=True)


def service_notice_chat_id() -> str:
    return os.environ.get('AGENTD_CHAT_ID', '').strip()


def prepare_restart_notice(config: AgentdConfig, chat_id: str) -> None:
    if not chat_id:
        return
    write_startup_notice(
        config,
        {
            'chat_id': chat_id,
            'text': STARTED_NOTICE_TEXT,
            'created_at': time.time(),
        },
    )
    send_service_notice(config, chat_id, RESTARTING_NOTICE_TEXT)


def send_service_notice(config: AgentdConfig, chat_id: str, text: str) -> bool:
    if not chat_id or not text:
        return False
    try:
        from .feishu import FeishuApi

        FeishuApi(config.feishu).send_text(chat_id, text)
    except Exception as exc:
        print(f'failed to send Feishu service notice: {exc}', file=sys.stderr)
        return False
    return True


def launch_service_command(
    config: AgentdConfig,
    backend: str,
    command: str,
    *,
    delay_seconds: float = 0,
    timeout_seconds: int = 10,
) -> None:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    script = (
        'import subprocess, sys, time\n'
        'time.sleep(float(sys.argv[1]))\n'
        'raise SystemExit(subprocess.call(sys.argv[2:]))\n'
    )
    args = [
        sys.executable,
        '-c',
        script,
        str(delay_seconds),
        str(agentd_executable(config)),
        '--config',
        str(config.config_path),
        'service',
        command,
        '--backend',
        backend,
    ]
    if command in {'restart', 'stop'}:
        args.extend(['--timeout', str(timeout_seconds)])
    with service_log_path(config).open('a', encoding='utf-8') as log:
        subprocess.Popen(
            args,
            cwd=config.workspace,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )


def launch_idle_service_command(
    config: AgentdConfig,
    backend: str,
    command: str,
    *,
    not_before: float,
    timeout_seconds: int = 10,
) -> None:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    script = (
        'import subprocess, sys, time\n'
        'from pathlib import Path\n'
        'from agentd.registry import Registry\n'
        'db_path = Path(sys.argv[1])\n'
        'not_before = float(sys.argv[2])\n'
        'poll_seconds = float(sys.argv[3])\n'
        'idle_grace_seconds = float(sys.argv[4])\n'
        'cmd = sys.argv[5:]\n'
        'time.sleep(max(0, not_before - time.time()))\n'
        'idle_since = None\n'
        'while True:\n'
        '    try:\n'
        '        idle = Registry(db_path).idle_work_count() == 0\n'
        '    except Exception as exc:\n'
        '        print(f\"waiting for agentd idle failed: {exc}\", flush=True)\n'
        '        idle = False\n'
        '    now = time.time()\n'
        '    if idle:\n'
        '        idle_since = idle_since or now\n'
        '        if now - idle_since >= idle_grace_seconds:\n'
        '            break\n'
        '    else:\n'
        '        idle_since = None\n'
        '    time.sleep(poll_seconds)\n'
        'raise SystemExit(subprocess.call(cmd))\n'
    )
    args = [
        sys.executable,
        '-c',
        script,
        str(config.db_path),
        str(not_before),
        '1',
        '3',
        str(agentd_executable(config)),
        '--config',
        str(config.config_path),
        'service',
        command,
        '--backend',
        backend,
    ]
    if command in {'restart', 'stop'}:
        args.extend(['--timeout', str(timeout_seconds)])
    with service_log_path(config).open('a', encoding='utf-8') as log:
        subprocess.Popen(
            args,
            cwd=config.workspace,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )


def agentd_executable(config: AgentdConfig) -> Path:
    return config.executable


def pid_path(config: AgentdConfig) -> Path:
    return _state_dir(config) / 'agentd.pid'


def service_log_path(config: AgentdConfig) -> Path:
    return config.log_dir / SERVICE_LOG_NAME


def _state_dir(config: Any) -> Path:
    try:
        value = config.state_dir
    except AttributeError:
        value = config.runtime_dir
    return Path(value)


def read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        return 0


def pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not pid_is_zombie(pid)


def pid_is_zombie(pid: int) -> bool:
    stat_path = Path('/proc') / str(pid) / 'stat'
    try:
        stat = stat_path.read_text(encoding='utf-8')
    except OSError:
        return False
    parts = stat.split()
    return len(parts) > 2 and parts[2] == 'Z'


def print_tail(path: Path, lines: int) -> None:
    for line in tail_lines(path, lines):
        print(line)


def tail_lines(path: Path, lines: int) -> list[str]:
    if lines <= 0:
        return []
    try:
        return path.read_text(encoding='utf-8', errors='replace').splitlines()[-lines:]
    except OSError:
        return []


def follow_file(path: Path) -> None:
    with path.open('r', encoding='utf-8', errors='replace') as fh:
        fh.seek(0, os.SEEK_END)
        while True:
            line = fh.readline()
            if line:
                print(line, end='')
                continue
            time.sleep(0.5)
