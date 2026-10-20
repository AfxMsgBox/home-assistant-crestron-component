"""部署工具共用的启动、SSH、服务器备份和 HTTP 恢复检查。仅使用标准库。"""

import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata
from urllib.parse import urlsplit


class DeployError(Exception):
    """可直接展示给用户的部署错误。"""


def styled(text, code, stream=None):
    """只在终端启用颜色；日志文件和 NO_COLOR 环境保持纯文本。"""
    stream = sys.stdout if stream is None else stream
    if stream.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb':
        return f'\033[{code}m{text}\033[0m'
    return text


def message(text, level='info', *, file=None):
    stream = sys.stdout if file is None else file
    label, code = {'info': ('信息', '36'), 'step': ('进行', '1;34'),
                   'success': ('完成', '32'), 'warning': ('警告', '33'),
                   'error': ('错误', '31')}[level]
    print(f'{styled("[" + label + "]", code, stream)} {text}', file=stream, flush=True)


def heading(text):
    print('\n' + styled(f'── {text} ──', '1;34'), flush=True)


def display_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1
               for c in str(text))


def details(rows):
    """先按中文显示宽度补齐标签，再上色，避免 ANSI 码影响对齐。"""
    rows = list(rows)
    width = max((display_width(label) for label, _ in rows), default=0)
    for label, value in rows:
        key = str(label) + ' ' * (width - display_width(label))
        print(f'       {styled(key, "36")} : {value}', flush=True)


def table(headers, rows, *, right=()):
    """按显示宽度对齐，控制字符转为可见文本，完整保留名称和 ID。"""
    def cell(value):
        return ''.join(c if c.isprintable() else repr(c)[1:-1] for c in str(value))

    rows = [[cell(value) for value in row] for row in [headers, *rows]]
    widths = [max(display_width(row[i]) for row in rows) for i in range(len(headers))]
    for index, row in enumerate(rows):
        cells = []
        for i, value in enumerate(row):
            padding = ' ' * (widths[i] - display_width(value))
            cells.append(padding + value if i in right else value + padding)
        line = '  '.join(cells).rstrip()
        print('  ' + (styled(line, '1;36') if index == 0 else line), flush=True)
        if index == 0:
            print('  ' + styled('  '.join('─' * width for width in widths), '36'), flush=True)


def file_table(entries):
    """entries 为 (文件名, 字节数, 修改时间戳)，统一使用 UTC，避免时区歧义。"""
    table(('文件', '大小', '最后修改时间（UTC）'),
          [(name, f'{size:,} B', datetime.fromtimestamp(mtime, timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
           for name, size, mtime in entries], right=(1,))


def local_files(paths, *, base=None):
    entries = []
    for path in paths:
        path = Path(path)
        stat = path.stat()
        entries.append((path.relative_to(base) if base else path.name, stat.st_size, stat.st_mtime))
    file_table(entries)


def launch(stage, argv):
    """独立 Python 入口复用单文件启动器，避免重复下载或混用来源。"""
    script = Path(__file__).with_name('deploy_to_ha.sh')
    if not script.is_file():
        raise DeployError('缺少 deploy_to_ha.sh，请下载完整源码，或单独运行该 Shell 启动器')
    option = {'devices': '--devices-only', 'dashboards': '--dashboard-only',
              'automations': '--automations-only'}[stage]
    # 不使用 exec：让最外层 Shell 的 EXIT 清理始终有机会执行。
    return subprocess.call(['bash', str(script), option, *argv])


def add_connection_args(parser):
    parser.add_argument('--host', required=True)
    parser.add_argument('--user', default='root')
    parser.add_argument('--port', type=int, default=22)
    parser.add_argument('--identity')
    parser.add_argument('--ha-url', default=os.environ.get('HA_URL', ''))


def run(command, *, capture=False, timeout=None, message='命令执行失败', env=None):
    result = subprocess.run(command, text=True, capture_output=capture, timeout=timeout, env=env)
    if result.returncode:
        raise DeployError(f'{message}（退出码 {result.returncode}）')
    return result.stdout if capture else None


def require(program):
    if not shutil.which(program):
        raise DeployError(f'找不到 {program} 命令')


def ask_yes_no(prompt, *, default=False, read=None):
    native_input = read is None
    read = input if read is None else read
    hint = '[Y/n]' if default else '[y/N]'
    while True:
        try:
            answer = read(styled('[选择]', '1;33') + f' {prompt} {hint} ').strip().lower()
        except EOFError:
            if native_input and not sys.stdin.isatty():
                print(flush=True)
            return False
        if native_input and not sys.stdin.isatty():
            print(flush=True)
        if not answer:
            return default
        if answer in ('y', 'yes', '是'):
            return True
        if answer in ('n', 'no', '否'):
            return False
        message('请输入 y（是）或 n（否），回车使用提示中的默认值。', 'warning')


class Connection:
    def __init__(self, args):
        for label, value in [('服务器地址', args.host), ('用户名', args.user)]:
            if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._-]*', value):
                raise DeployError(f'{label}包含不支持的字符')
        if not 1 <= args.port <= 65535:
            raise DeployError('SSH 端口必须在 1..65535')
        require('ssh')
        options = ['-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=accept-new']
        if args.identity:
            if not Path(args.identity).is_file():
                raise DeployError('私钥文件不存在')
            options += ['-i', args.identity]
        self.env = dict(os.environ)
        password = self.env.get('HA_SSH_PASSWORD', '')
        prefix = []
        if password:
            require('sshpass')
            self.env['SSHPASS'] = password
            prefix = ['sshpass', '-e']
            options += ['-o', 'PubkeyAuthentication=no', '-o',
                        'PreferredAuthentications=password,keyboard-interactive']
        else:
            options += ['-o', 'BatchMode=yes', '-o', 'PasswordAuthentication=no',
                        '-o', 'KbdInteractiveAuthentication=no']
        self.host = args.host
        self.target = f'{args.user}@{args.host}'
        self.ssh = prefix + ['ssh', '-p', str(args.port), *options, self.target]
        # -q 关闭进度百分比、速度和预计耗时；失败仍检查退出码并报告。
        self.scp = prefix + ['scp', '-q', '-O', '-P', str(args.port), *options]

    def remote(self, script, *, capture=False, timeout=180, message='SSH 操作失败'):
        return run([*self.ssh, script], capture=capture, timeout=timeout,
                   message=message, env=self.env)

    def check(self):
        message(f'正在测试 SSH 连接：{self.target}', 'step')
        self.remote('test -d /homeassistant')


BACKUP_PATTERN = r'/homeassistant/crestron-backups/[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9]{6}-[A-Za-z0-9]+'


def validate_backup(path):
    if not re.fullmatch(BACKUP_PATTERN, path):
        raise DeployError('服务器返回的备份目录无效，已停止部署')
    return path


def create_backup(command, env=None):
    script = ('set -eu; umask 077; stamp=$(date +%Y-%m-%d/%H%M%S); '
              'parent="/homeassistant/crestron-backups/${stamp%/*}"; mkdir -p "$parent"; '
              'mktemp -d "$parent/${stamp#*/}-XXXXXX"')
    path = run([*command, script], capture=True, timeout=45, env=env,
               message='无法创建服务器备份目录').strip()
    validate_backup(path)
    message('已创建本次服务器备份目录', 'success')
    details([('备份目录', path)])
    return path


def core_url(connection, override=''):
    if override:
        url = override.rstrip('/')
    else:
        info = json.loads(connection.remote('ha core info --raw-json', capture=True))
        core = info.get('data', info)
        port = core.get('port')
        if type(port) is not int or not 1 <= port <= 65535:
            raise DeployError('无法识别 HA HTTP 端口，请使用 --ha-url')
        scheme = 'https' if core.get('ssl') else 'http'
        url = f'{scheme}://{connection.host}:{port}'
    parts = urlsplit(url)
    if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        raise DeployError('HA 地址必须是无凭据和查询参数的 HTTP(S) 地址')
    connection.remote('command -v curl >/dev/null 2>&1', message='服务器端缺少 curl 或 SSH 连接失败')
    return url


def wait_for_http(connection, url, seconds):
    url = url.rstrip('/') + '/api/'
    command = "curl -sS --connect-timeout 3 --max-time 5 -o /dev/null -w '%{http_code}' -- " + shlex.quote(url)
    message('正在通过 SSH 在服务器端检查 Home Assistant HTTP 接口', 'step')
    details([('探测地址', url), ('最长等待', f'{seconds} 秒')])
    start = time.monotonic()
    while True:
        probe = subprocess.run([*connection.ssh[:-1], '-o', 'ServerAliveInterval=5',
                                '-o', 'ServerAliveCountMax=1', connection.target, command],
                               capture_output=True, text=True, env=connection.env, timeout=30)
        status = probe.stdout.strip()
        if probe.returncode == 0 and status in ('200', '401'):
            message(f'Home Assistant HTTP 接口已恢复（HTTP {status}）', 'success')
            return
        elapsed = time.monotonic() - start
        message(f'已等待 {int(elapsed):>3}/{seconds} 秒，HTTP 状态：{status or "无响应"}，检查退出码：{probe.returncode}')
        if probe.stderr:
            print(probe.stderr, file=sys.stderr, flush=True)
        if elapsed >= seconds:
            raise DeployError(f'已发送 HA 服务重启命令，但 HTTP 接口未在 {seconds} 秒内恢复（{url}）；请查看 ha core logs')
        time.sleep(min(2, max(0, seconds - elapsed)))


def cli(entry):
    try:
        return entry()
    except (DeployError, OSError, ValueError, subprocess.SubprocessError) as error:
        message(f'错误：{error}', 'error', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        message('已取消部署。', 'warning', file=sys.stderr)
        return 130
