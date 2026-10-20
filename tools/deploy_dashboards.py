#!/usr/bin/env python3
"""独立部署仪表盘及卡片资源；不上传设备配置、不重启 HA。"""

import argparse
import json
from pathlib import Path
import sys

import deploy_common as common


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--prepared' not in argv:
        return common.launch('dashboards', argv)
    argv.remove('--prepared')
    parser = argparse.ArgumentParser(description=__doc__)
    common.add_connection_args(parser)
    parser.add_argument('--source-root', required=True)
    parser.add_argument('--dashboard-dir')
    parser.add_argument('--context')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--restart-timeout', type=int, default=120)
    args = parser.parse_args(argv)
    if not 1 <= args.restart_timeout <= 9999:
        parser.error('--restart-timeout 必须在 1..9999 之间')
    root = Path(args.source_root)
    directory = Path(args.dashboard_dir) if args.dashboard_dir else root / 'dashboard'
    tool = root / 'tools/deploy_dashboards.mjs'
    if not directory.is_dir() or not tool.is_file():
        raise common.DeployError('所选源码不包含 dashboard 或页面部署工具')
    common.require('node')
    common.run(['node', '-e',
                'if (Number(process.versions.node.split(".")[0]) < 22 || typeof WebSocket !== "function") process.exit(1)'],
               message='页面部署需要 Node.js 22 或更新版本')
    options = ['node', str(tool), '--dir', str(directory)]
    common.run([*options, '--check', '--remote-backup-dir', '/homeassistant/crestron-backups'],
               message='页面或资源校验失败')
    if args.check:
        return 0
    context = json.loads(Path(args.context).read_text()) if args.context else {}
    connection = common.Connection(args)
    connection.check()
    # 单独首次安装卡片时，HA 可能尚未注册 /local 路由。
    has_resources = any(json.loads(file.read_text()).get('resources') for file in directory.rglob('*.json'))
    if has_resources:
        probe = connection.remote(
            'set -eu; if test -d /homeassistant/www; then echo existing; else echo missing; fi',
            capture=True).strip()
        if probe not in ('existing', 'missing'):
            raise common.DeployError('无法确认 HA 静态资源目录，尚未部署卡片')
        if probe == 'missing':
            if not common.ask_yes_no('HA 尚无 www 目录，首次安装卡片需要初始化并重启 Core，是否继续？', default=True):
                raise common.DeployError('已取消初始化，未部署页面；请创建 www 并重启 Core 后重试')
            url = common.core_url(connection, args.ha_url or context.get('ha_url', ''))
            connection.remote('ha core check', message='HA 配置检查失败，尚未初始化资源目录')
            connection.remote('mkdir -p /homeassistant/www')
            connection.remote('ha core restart', message='首次资源初始化后的 Core 重启失败')
            common.wait_for_http(connection, url, args.restart_timeout)
    backup = (common.validate_backup(context['backup_dir']) if context.get('backup_dir')
              else common.create_backup(connection.ssh, connection.env))
    options += ['--remote-backup-dir', backup + '/dashboard', '--host', args.host,
                '--wait-seconds', str(args.restart_timeout)]
    url = args.ha_url or context.get('ha_url')
    if url:
        options += ['--ha-url', url]
    common.run([*options, '--', *connection.ssh], env=connection.env, message='仪表盘部署失败')
    common.message('页面部署完成；已打开的浏览器请刷新页面。', 'success')
    return 0


if __name__ == '__main__':
    sys.exit(common.cli(main))
