#!/usr/bin/env python3
"""设备生成、组件部署、配置检查与 Core 重启。"""

import argparse
import json
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

import deploy_common as common


def deploy(args):
    common.heading('部署组件和设备配置')
    root = Path(args.source_root)
    component = root / 'custom_components/crestron'
    converter = root / 'tools/xlsx_to_yaml.py'
    for file in (component / 'manifest.json', converter):
        if not file.is_file():
            raise common.DeployError(f'所选源码缺少 {file}')
    if args.generate_yaml:
        if not args.xlsx or not Path(args.xlsx).is_file():
            raise common.DeployError('必须提供存在的 xlsx 文件')
        Path(args.yaml).parent.mkdir(parents=True, exist_ok=True)
        common.message(f'正在生成 YAML：{args.yaml}', 'step')
        common.run([sys.executable, str(converter), args.xlsx, args.yaml], message='设备 YAML 生成失败')
    if args.upload_yaml is None:
        args.upload_yaml = common.ask_yes_no(f'是否上传 {args.yaml} 到目标 HA 服务器（存在则覆盖）？', default=True)
    if args.upload_yaml and not Path(args.yaml).is_file():
        raise common.DeployError(f'要上传的 YAML 不存在：{args.yaml}')
    common.require('scp')
    connection = common.Connection(args)
    common.details([('目标服务器', connection.target), ('设备配置', args.yaml),
                    ('上传设备', '是（存在则覆盖）' if args.upload_yaml else '否')])
    connection.check()
    if args.update:
        connection.remote("test -f '/homeassistant/custom_components/crestron/manifest.json'",
                          message='目标尚未安装 Crestron，请使用 --install')
    url = common.core_url(connection, args.ha_url)
    if args.require_automations:
        connection.remote('command -v jq >/dev/null 2>&1', message='自动化关联需要服务器 jq')
    backup = common.create_backup(connection.ssh, connection.env)

    # 本机只准备运行组件；独立 YAML 和缓存不随组件复制。
    staging_parent = Path(args.result_file).parent if args.result_file else None
    with tempfile.TemporaryDirectory(prefix='crestron-devices-', dir=staging_parent) as temp:
        staged = Path(temp) / 'crestron'
        shutil.copytree(component, staged, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'crestron.yaml'))
        common.message('正在备份远端组件...', 'step')
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
        # HA 启动时才注册 /local 路由；完整安装在本次重启前准备好 www。
        connection.remote("mkdir -p -- '/homeassistant/custom_components' '/homeassistant/www'")
        common.message('正在上传组件', 'step')
        common.details([('本地目录', component), ('目标位置', connection.target + ':/homeassistant/custom_components/crestron/')])
        common.local_files(sorted(p for p in staged.rglob('*') if p.is_file()), base=staged)
        common.run([*connection.scp, '-p', '-r', str(staged),
                    connection.target + ':/homeassistant/custom_components/'],
                   env=connection.env, message='组件上传失败')
        common.message('组件文件上传完成', 'success')
        if args.upload_yaml:
            common.message('正在上传设备配置', 'step')
            common.local_files([args.yaml])
            common.run([*connection.scp, '-p', str(Path(args.yaml).resolve()),
                        connection.target + ':/homeassistant/custom_components/crestron/crestron.yaml'],
                       env=connection.env, message='设备 YAML 上传失败')
            common.message('设备配置上传完成', 'success')
        else:
            common.message('已按用户选择跳过 YAML 上传。')
    common.message('正在检查 Home Assistant 配置（ha core check）...', 'step')
    connection.remote('ha core check', message='配置检查未通过，已跳过重启。远端文件已更新，请修正配置或从备份恢复')
    common.message('正在重启 Home Assistant 服务...', 'step')
    connection.remote('ha core restart', message='Home Assistant 服务重启失败')
    common.wait_for_http(connection, url, args.restart_timeout)
    if args.result_file:
        # 仅在设备部署成功后交给后续步骤；不包含密码或令牌。
        Path(args.result_file).write_text(json.dumps({'backup_dir': backup, 'ha_url': url}), encoding='utf-8')
    common.message('设备部署完成，HA HTTP 接口已恢复。', 'success')
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
    parser.add_argument('--require-automations', action='store_true')
    parser.add_argument('--restart-timeout', type=int, default=120)
    parser.add_argument('--result-file')
    args = parser.parse_args(argv)
    if not 1 <= args.restart_timeout <= 9999:
        parser.error('--restart-timeout 必须在 1..9999 之间')
    return deploy(args)


if __name__ == '__main__':
    sys.exit(common.cli(main))
