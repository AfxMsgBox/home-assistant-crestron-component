"""两步部署共用的启动、SSH、服务器备份和 HTTP 恢复检查。仅使用标准库。"""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit


class DeployError(Exception):
    """可直接展示给用户的部署错误。"""


def launch(stage, argv):
    """独立 Python 入口复用单文件启动器，避免重复下载或混用来源。"""
    script = Path(__file__).with_name('deploy_to_ha.sh')
    if not script.is_file():
        raise DeployError('缺少 deploy_to_ha.sh，请下载完整源码，或单独运行该 Shell 启动器')
    option = '--devices-only' if stage == 'devices' else '--automations-only'
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


def ask_yes_no(prompt):
    while True:
        try:
            answer = input(prompt + ' [y/N] ').strip().lower()
        except EOFError:
            return False
        if answer in ('y', 'yes', '是'):
            return True
        if answer in ('', 'n', 'no', '否'):
            return False
        print('请输入 y（是）或 n（否）。', flush=True)


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
        self.scp = prefix + ['scp', '-O', '-P', str(args.port), *options]

    def remote(self, script, *, capture=False, timeout=180, message='SSH 操作失败'):
        return run([*self.ssh, script], capture=capture, timeout=timeout,
                   message=message, env=self.env)

    def check(self):
        print(f'正在测试 SSH 连接：{self.target}', flush=True)
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
    print(f'本次服务器备份目录：{path}', flush=True)
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
    print(f'正在通过 SSH 在服务器端检查 Home Assistant HTTP 接口：{url}（最长等待 {seconds} 秒）', flush=True)
    start = time.monotonic()
    while True:
        probe = subprocess.run([*connection.ssh[:-1], '-o', 'ServerAliveInterval=5',
                                '-o', 'ServerAliveCountMax=1', connection.target, command],
                               capture_output=True, text=True, env=connection.env, timeout=30)
        status = probe.stdout.strip()
        if probe.returncode == 0 and status in ('200', '401'):
            return
        elapsed = time.monotonic() - start
        print(f'已等待 {int(elapsed)}/{seconds} 秒，HTTP 状态：{status or "无响应"}，检查退出码：{probe.returncode}', flush=True)
        if probe.stderr:
            print(probe.stderr, file=sys.stderr, flush=True)
        if elapsed >= seconds:
            raise DeployError(f'已发送 HA 服务重启命令，但 HTTP 接口未在 {seconds} 秒内恢复（{url}）；请查看 ha core logs')
        time.sleep(min(2, max(0, seconds - elapsed)))


def cli(entry):
    try:
        return entry()
    except (DeployError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'错误：{error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('已取消部署。', file=sys.stderr)
        return 130
