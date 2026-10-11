#!/usr/bin/env python3
"""第一步：设备生成、组件及仪表盘部署、配置检查与 Core 重启。"""

import argparse
import json
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

import deploy_common as common


def deploy(args):
    root = Path(args.source_root)
    component = root / 'custom_components/crestron'
    converter = root / 'tools/xlsx_to_yaml.py'
    for file in (component / 'manifest.json', converter):
        if not file.is_file():
            raise common.DeployError(f'所选源码缺少 {file}')
    dashboards = Path(args.dashboard_dir) if args.dashboard_dir else root / 'dashboard'
    dashboard_tool = root / 'tools/deploy_dashboards.mjs'
    if not dashboards.is_dir() and not args.dashboard_dir and not args.dashboard_only:
        print('所选源码不包含 dashboard，跳过页面部署。', flush=True)
        args.skip_dashboard = True
    if not args.skip_dashboard:
        if not dashboards.is_dir() or not dashboard_tool.is_file():
            raise common.DeployError('所选源码不包含 dashboard 或页面部署工具')
        common.require('node')
        common.run(['node', '-e', 'if (Number(process.versions.node.split(".")[0]) < 22 || typeof WebSocket !== "function") process.exit(1)'],
                   message='页面部署需要 Node.js 22 或更新版本')
        common.run(['node', str(dashboard_tool), '--dir', str(dashboards), '--check',
                    '--remote-backup-dir', '/homeassistant/crestron-backups'],
                   message='页面校验失败或所选分支的页面工具不支持服务器备份')
    if args.generate_yaml:
        if not args.xlsx or not Path(args.xlsx).is_file():
            raise common.DeployError('必须提供存在的 xlsx 文件')
        Path(args.yaml).parent.mkdir(parents=True, exist_ok=True)
        print(f'正在生成 YAML：{args.yaml}', flush=True)
        common.run([sys.executable, str(converter), args.xlsx, args.yaml], message='设备 YAML 生成失败')
    if args.upload_yaml is None:
        args.upload_yaml = common.ask_yes_no(f'是否上传 {args.yaml} 到目标 HA 服务器？')
    if args.upload_yaml and not Path(args.yaml).is_file():
        raise common.DeployError(f'要上传的 YAML 不存在：{args.yaml}')
    if not args.dashboard_only:
        common.require('scp')
    connection = common.Connection(args)
    connection.check()
    if args.update and not args.dashboard_only:
        connection.remote("test -f '/homeassistant/custom_components/crestron/manifest.json'",
                          message='目标尚未安装 Crestron，请使用 --install')
    url = '' if args.dashboard_only else common.core_url(connection, args.ha_url)
    if args.require_automations:
        connection.remote('command -v jq >/dev/null 2>&1', message='自动化关联需要服务器 jq')
    backup = common.create_backup(connection.ssh, connection.env)

    def deploy_dashboards():
        if args.skip_dashboard:
            print('已跳过仪表盘部署。', flush=True)
            return
        options = ['node', str(dashboard_tool), '--dir', str(dashboards),
                   '--remote-backup-dir', backup + '/dashboard', '--host', args.host]
        if args.ha_url:
            options += ['--ha-url', args.ha_url]
        common.run([*options, '--', *connection.ssh], env=connection.env, message='仪表盘部署失败')

    if args.dashboard_only:
        deploy_dashboards()
        print('页面部署完成，无需重启 Home Assistant。', flush=True)
        return 0
    print('第一步：部署组件和设备配置', flush=True)
    # 本机只准备运行组件；独立 YAML 和缓存不随组件复制。
    staging_parent = Path(args.result_file).parent if args.result_file else None
    with tempfile.TemporaryDirectory(prefix='crestron-devices-', dir=staging_parent) as temp:
        staged = Path(temp) / 'crestron'
        shutil.copytree(component, staged, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'crestron.yaml'))
        print('正在备份远端组件...', flush=True)
        script = 'backup_dir=' + shlex.quote(backup) + '; ' + '''set -eu; umask 077
if test -d /homeassistant/custom_components/crestron; then
    partial="$backup_dir/crestron.tar.gz.part"
    trap 'rm -f -- "$partial"' EXIT
    trap 'exit 1' HUP INT TERM
    tar -czf "$partial" -C /homeassistant/custom_components crestron
    tar -tzf "$partial" >/dev/null
    mv "$partial" "$backup_dir/crestron.tar.gz"
    echo "组件备份：$backup_dir/crestron.tar.gz"
else
    echo "首次安装，无旧组件需要备份。"
fi'''
        connection.remote(script, message='组件备份失败，尚未覆盖组件')
        connection.remote("mkdir -p -- '/homeassistant/custom_components'")
        print(f'正在上传组件：{component} -> {connection.target}', flush=True)
        common.run([*connection.scp, '-p', '-r', str(staged),
                    connection.target + ':/homeassistant/custom_components/'],
                   env=connection.env, message='组件上传失败')
        if args.upload_yaml:
            common.run([*connection.scp, '-p', str(Path(args.yaml).resolve()),
                        connection.target + ':/homeassistant/custom_components/crestron/crestron.yaml'],
                       env=connection.env, message='设备 YAML 上传失败')
        else:
            print('已按用户选择跳过 YAML 上传。', flush=True)
    print('正在检查 Home Assistant 配置（ha core check）...', flush=True)
    connection.remote('ha core check', message='配置检查未通过，已跳过重启。远端文件已更新，请修正配置或从备份恢复')
    deploy_dashboards()
    print('正在重启 Home Assistant 服务...', flush=True)
    connection.remote('ha core restart', message='Home Assistant 服务重启失败')
    common.wait_for_http(connection, url, args.restart_timeout)
    if args.result_file:
        # 仅在第一步成功后交给第二步；不包含密码或令牌。
        Path(args.result_file).write_text(json.dumps({'backup_dir': backup, 'ha_url': url}), encoding='utf-8')
    print('设备部署完成，HA HTTP 接口已恢复。', flush=True)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--prepared' not in argv:
        return common.launch('devices', argv)
    argv.remove('--prepared')
    parser = argparse.ArgumentParser(description=__doc__)
    common.add_connection_args(parser)
    parser.add_argument('--source-root', required=True)
    parser.add_argument('--yaml', required=True)
    parser.add_argument('--xlsx')
    parser.add_argument('--generate-yaml', action='store_true')
    upload = parser.add_mutually_exclusive_group()
    upload.add_argument('--upload-yaml', action='store_true', default=None)
    upload.add_argument('--skip-upload-yaml', action='store_false', dest='upload_yaml')
    parser.add_argument('--update', action='store_true')
    parser.add_argument('--dashboard-dir')
    parser.add_argument('--skip-dashboard', action='store_true')
    parser.add_argument('--dashboard-only', action='store_true')
    parser.add_argument('--require-automations', action='store_true')
    parser.add_argument('--restart-timeout', type=int, default=120)
    parser.add_argument('--result-file')
    args = parser.parse_args(argv)
    if not 1 <= args.restart_timeout <= 9999:
        parser.error('--restart-timeout 必须在 1..9999 之间')
    if args.dashboard_only and (args.skip_dashboard or args.generate_yaml or args.upload_yaml):
        parser.error('--dashboard-only 不能与生成、上传 YAML 或跳过页面同用')
    return deploy(args)


if __name__ == '__main__':
    sys.exit(common.cli(main))
