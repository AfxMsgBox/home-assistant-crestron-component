#!/usr/bin/env python3
"""读取 HA 注册信息、展示关联、生成并按用户选择上传自动化。"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlencode, urlsplit

import xlsx_to_automations as generator
import deploy_common as common

MANUAL = '/homeassistant/automations_manual.yaml'
CONFIG = '/homeassistant/configuration.yaml'


def remote(command, script, content=None, timeout=45):
    result = subprocess.run([*command, script], input=content, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        # 不回显命令和响应正文：认证失败时不能把凭据带进日志。
        raise generator.AssociationError(f'SSH 操作失败（退出码 {result.returncode}），请检查连接、权限和服务器工具')
    return result.stdout


class Api:
    """所有 HTTP 请求经 SSH 在服务器执行，凭据仅通过标准输入/内存传递。"""

    def __init__(self, command, url):
        parts = urlsplit(url)
        if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment):
            raise generator.AssociationError('HA 地址必须是不带凭据和查询参数的 HTTP(S) 地址')
        self.command, self.url = command, url.rstrip('/') + '/'
        self.token = os.environ.get('HA_TOKEN')
        if not self.token:
            session = json.loads(remote(command, '''jq -ce '.data as $d |
($d.users | map(select(.is_owner and .is_active)) | first | .id) as $owner |
($d.refresh_tokens | map(select(.user_id == $owner and .token_type == "normal")) |
sort_by(.created_at) | last) as $r |
if $r == null then error("没有可用的所有者会话") else {token:$r.token,client_id:$r.client_id} end' /homeassistant/.storage/auth'''))
            form = urlencode(dict(grant_type='refresh_token', refresh_token=session['token'],
                                  client_id=session['client_id']))
            response = self.request('auth/token', form=form)
            self.token = response.get('access_token')
            if not isinstance(self.token, str) or not self.token:
                raise generator.AssociationError('未能取得 HA 临时令牌，请设置管理员 HA_TOKEN')

    def request(self, endpoint, form=None, post=False):
        config = ['url = ' + json.dumps(self.url + endpoint), 'request = "POST"' if post or form is not None else 'request = "GET"']
        if self.token:
            if any(c in self.token for c in '\r\n'):
                raise generator.AssociationError('HA_TOKEN 格式无效')
            config.append('header = ' + json.dumps('Authorization: Bearer ' + self.token))
        if form is not None:
            config += ['header = "Content-Type: application/x-www-form-urlencoded"',
                       'data = ' + json.dumps(form)]
        elif post:
            config += ['header = "Content-Type: application/json"', 'data = "{}"']
        raw = remote(self.command, 'curl -fsS --connect-timeout 5 --max-time 30 --config -',
                     ('\n'.join(config) + '\n').encode())
        return json.loads(raw)


def read_registry(command, api):
    # 只读 .storage；不读取不需要的设备连接信息/集成参数，不修改注册表。
    script = '''jq -n \
--slurpfile e /homeassistant/.storage/core.entity_registry \
--slurpfile d /homeassistant/.storage/core.device_registry \
--slurpfile a /homeassistant/.storage/core.area_registry \
--slurpfile f /homeassistant/.storage/core.floor_registry '
{entities: [$e[0].data.entities[] | {entity_id,platform,unique_id,name,original_name,device_id,area_id,disabled_by,has_entity_name}],
 devices: [$d[0].data.devices[] | {id,name,name_by_user,area_id}],
 areas: [$a[0].data.areas[] | {id,name,floor_id}],
 floors: [$f[0].data.floors[] | {id:.floor_id,name}]}' '''
    snapshot = json.loads(remote(command, script))
    snapshot['states'] = api.request('api/states')
    return snapshot


def read_optional(command, path):
    q = shlex.quote(path)
    raw = remote(command, f'if test -f {q}; then jq -Rs . < {q}; else printf null; fi')
    value = json.loads(raw)
    return None if value is None else value.encode('utf-8')


def include_config(content):
    """只处理独立的顶层 include；保留文件其余字节和手工注释，不重写 YAML。"""
    if content is None:
        raise generator.AssociationError('服务器 configuration.yaml 不存在')
    text = content.decode('utf-8')
    includes = re.findall(r'(?m)^[^#\n]*!include\s+[\'"]?automations_manual\.yaml[\'"]?\s*(?:#.*)?$', text)
    desired = re.findall(r'(?m)^automation manual:\s*!include\s+[\'"]?automations_manual\.yaml[\'"]?\s*(?:#.*)?$', text)
    if len(desired) == 1 and len(includes) == 1:
        return content
    if includes or re.search(r'(?m)^[\'"]?automation manual[\'"]?\s*:', text):
        raise generator.AssociationError('自动化 include 存在冲突或复杂写法，请手工整理 automation manual 引用后重试')
    return (text.rstrip('\n') + '\n\nautomation manual: !include automations_manual.yaml\n').encode()


def digest(content):
    return 'absent' if content is None else hashlib.sha256(content).hexdigest()


def install_script(backup, old_config, old_manual, update_config):
    if not re.fullmatch(r'/homeassistant/crestron-backups/\d{4}-\d{2}-\d{2}/\d{6}-[A-Za-z0-9]+', backup):
        raise generator.AssociationError('服务器备份目录无效')
    # 下载前的内容摘要在覆盖前再检查；失败停止，避免覆盖用户刚刚修改的文件。
    return f'''set -eu
umask 077
backup_dir={shlex.quote(backup + '/automation')}
config={shlex.quote(CONFIG)}
manual={shlex.quote(MANUAL)}
check_hash() {{
  test ! -L "$1" || return 1
  if test -f "$1"; then actual=$(sha256sum "$1"); actual=${{actual%% *}}; else actual=absent; fi
  test "$actual" = "$2"
}}
check_hash "$config" {digest(old_config)}
check_hash "$manual" {digest(old_manual)}
# mkdir 不加 -p：同一次备份目录不能重复使用并覆盖原始备份。
mkdir "$backup_dir"
cp -p "$config" "$backup_dir/configuration.yaml"
if test -f "$manual"; then cp -p "$manual" "$backup_dir/automations_manual.yaml"; else touch "$backup_dir/manual-was-absent"; fi
stage=$(mktemp -d /homeassistant/.crestron-automation.XXXXXX)
changed=no
success=no
cleanup() {{
  code=$?
  if test "$changed" = yes && test "$success" != yes; then
    cp -p "$backup_dir/configuration.yaml" "$config" || code=1
    if test -f "$backup_dir/manual-was-absent"; then rm -f "$manual"; else cp -p "$backup_dir/automations_manual.yaml" "$manual" || code=1; fi
  fi
  rm -rf -- "$stage"
  exit "$code"
}}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
# 输入压缩包仅包含工具生成的两个固定文件名，不保存上传中间文件。
tar -xzf - -C "$stage"
test -f "$stage/automations_manual.yaml"
check_hash "$config" {digest(old_config)}
check_hash "$manual" {digest(old_manual)}
changed=yes
mv "$stage/automations_manual.yaml" "$manual"
{'mv "$stage/configuration.yaml" "$config"' if update_config else ':'}
ha core check
success=yes
'''


def upload(command, backup, content, old_config, old_manual, new_config, *, content_mtime=None):
    data = io.BytesIO()
    now = int(time.time())
    entries = []
    with tarfile.open(fileobj=data, mode='w:gz') as archive:
        files = {'automations_manual.yaml': content.encode()}
        if new_config != old_config:
            files['configuration.yaml'] = new_config
        for name, value in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(value), 0o600
            info.mtime = int(content_mtime) if name == 'automations_manual.yaml' and content_mtime is not None else now
            entries.append((name, info.size, info.mtime))
            archive.addfile(info, io.BytesIO(value))
    script = install_script(backup, old_config, old_manual, new_config != old_config)
    common.message('正在上传自动化配置并检查 HA 配置', 'step')
    common.file_table(entries)
    remote(command, script, data.getvalue(), timeout=180)


def show_associations(rows, snapshot, switch_map, occupancy_map):
    """从匹配后的映射生成表格，不解析报告句子，也不重新推断实体 ID。"""
    registry = generator.Registry(snapshot)
    targets = {registry.target(row): row['config'] for row in rows}
    groups = [('开关双向关联', '控制开关', switch_map), ('人在控制', '人在传感器', occupancy_map)]
    names = {'light': '灯光', 'switch': '插座', 'climate': '空调'}
    all_targets = set()
    for title, role, mapping in groups:
        for source, config in sorted(mapping.items()):
            ids = sorted(eid for values in config.values() for eid in values)
            all_targets.update(ids)
            common.heading(f'{title} · {len(ids)} 台设备')
            records = [(role, *registry.describe(source), source)]
            for eid in ids:
                cfg = targets[eid]
                records.append(('被控' + names[eid.split('.')[0]], cfg['floor'], cfg['room'], cfg['name'], eid))
            common.table(('角色', '楼层', '房间', '名称', '实体 ID'), records)
    common.details([('控制开关', len(switch_map)), ('人在传感器', len(occupancy_map)),
                    ('被控设备', len(all_targets))])
    if switch_map:
        common.message('开关双向同步：任意被控设备开启算开，全部关闭才算关。')
    if occupancy_map:
        common.message('人在控制：同一设备关联的传感器任意有人开启，全部无人持续 30 秒关闭。')
    if not all_targets:
        common.message('未填写有效关联；上传后会移除该文件中的旧关联。', 'warning')


def ask_upload(existing, add_include, read=None):
    changes = '覆盖服务器已有 automations_manual.yaml' if existing else '上传新的 automations_manual.yaml'
    if add_include:
        changes += '，并添加 configuration.yaml 引用'
    return common.ask_yes_no(f'{changes}，检查后重载自动化，是否继续？', default=True, read=read)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    split = argv.index('--') if '--' in argv else len(argv)
    command = argv[split + 1:]
    parser = generator.devices._ChineseArgumentParser(description=__doc__)
    parser.add_argument('--xlsx', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--template', default=str(generator.DEFAULT_TEMPLATE))
    parser.add_argument('--ha-url', required=True)
    parser.add_argument('--backup-dir')
    parser.add_argument('--wait-seconds', type=int, default=120)
    parser.add_argument('--upload', choices=('ask', 'yes', 'no'), default='ask')
    parser.add_argument('--export-registry', help='另外保存注册表快照到本地，便于离线运行生成器')
    args = parser.parse_args(argv[:split])
    if not command:
        parser.error('必须在 -- 后传入 SSH 命令')
    if not 0 <= args.wait_seconds <= 9999:
        parser.error('--wait-seconds 必须在 0..9999 之间')
    rows = generator.read_associations(args.xlsx)
    # 先校验模板占位格式，避免启动后才发现模板不可用。
    template = Path(args.template).read_text(encoding='utf-8')
    generator.render_template(template, {}, {}, [])
    if not re.search(r'(?m)^id:\s*[\'"]?crestron_manual_associations[\'"]?\s*(?:#.*)?$', template):
        raise generator.AssociationError('部署模板须保留固定 id: crestron_manual_associations')
    protected = {Path(args.template).resolve(), Path(args.xlsx).resolve()}
    generator.validate_output(args.output, args.template, args.xlsx)
    if args.export_registry and Path(args.export_registry).resolve() in protected | {Path(args.output).resolve()}:
        raise generator.AssociationError('注册表导出路径不能覆盖输入或自动化文件')
    api = Api(command, args.ha_url)
    deadline = time.monotonic() + args.wait_seconds
    common.heading('生成并部署自动化关联')
    common.details([('工作簿', args.xlsx), ('自动化输出', args.output)])
    common.message('正在读取 HA 注册表并等待关联实体完成注册/加载...', 'step')
    while True:
        snapshot = read_registry(command, api)
        try:
            switch_map, occupancy_map, report = generator.build_maps(rows, snapshot)
            break
        except generator.RegistryPending as error:
            if time.monotonic() >= deadline:
                raise generator.AssociationError(f'等待实体超时，未覆盖自动化；请检查第一阶段设备导入：\n{error}') from error
            common.message(f'{error}\n       5 秒后重试...', 'warning')
            time.sleep(min(5, max(0, deadline - time.monotonic())))
    content = generator.render_template(template, switch_map, occupancy_map, report)
    common.heading('关联清单')
    show_associations(rows, snapshot, switch_map, occupancy_map)
    generator.atomic_write(args.output, content)
    common.message(f'已生成 {args.output}，关联清单也已保存在文件顶部注释中。', 'success')
    if args.export_registry:
        generator.atomic_write(args.export_registry, json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n')
    if args.upload == 'no':
        common.message('已按选项跳过上传。')
        return 0
    old_config = read_optional(command, CONFIG)
    old_manual = read_optional(command, MANUAL)
    new_config = include_config(old_config)
    if args.upload == 'ask' and not ask_upload(old_manual is not None, new_config != old_config):
        common.message('已保留本地生成文件，跳过服务器自动化修改。')
        return 0
    if not args.backup_dir:
        # 独立运行在确认上传后才创建备份；完整流程复用设备部署的目录。
        args.backup_dir = common.create_backup(command)
    upload(command, args.backup_dir, content, old_config, old_manual, new_config,
           content_mtime=Path(args.output).stat().st_mtime)
    common.message('文件已上传，配置检查通过', 'success')
    common.details([('备份目录', f'{args.backup_dir}/automation/')])
    try:
        api.request('api/services/automation/reload', post=True)
    except (generator.AssociationError, ValueError, subprocess.TimeoutExpired) as error:
        raise generator.AssociationError('文件已上传但自动化重载未确认；请检查 HA 日志并手动重载，备份已保留') from error
    states = api.request('api/states')
    installed = [s for s in states if s.get('attributes', {}).get('id') == 'crestron_manual_associations']
    if len(installed) != 1 or installed[0].get('state') not in ('on', 'off'):
        raise generator.AssociationError('重载请求完成，但未确认自动化实体正常加载；请查看 HA 日志，勿重复创建')
    common.message('自动化部署完成', 'success')
    common.details([('实体 ID', installed[0]['entity_id']),
                    ('状态', '启用' if installed[0]['state'] == 'on' else '停用（保留原状态）')])
    return 0


def entry(argv=None):
    """公开入口复用来源选择；保留 -- 后传 SSH 命令的底层调试接口。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--prepared' not in argv:
        if '--' in argv:
            return main(argv)
        return common.launch('automations', argv)
    argv.remove('--prepared')
    parser = generator.devices._ChineseArgumentParser(description=__doc__)
    common.add_connection_args(parser)
    parser.add_argument('--xlsx', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--template', default=str(generator.DEFAULT_TEMPLATE))
    parser.add_argument('--wait-seconds', type=int, default=120)
    parser.add_argument('--upload', choices=('ask', 'yes', 'no'), default='ask')
    parser.add_argument('--context')
    args = parser.parse_args(argv)
    connection = common.Connection(args)
    connection.check()
    connection.remote('command -v jq >/dev/null 2>&1', message='自动化关联需要服务器 jq')
    context = json.loads(Path(args.context).read_text()) if args.context else {}
    url = common.core_url(connection, args.ha_url or context.get('ha_url', ''))
    options = ['--xlsx', args.xlsx, '--output', args.output, '--template', args.template,
               '--ha-url', url, '--wait-seconds', str(args.wait_seconds), '--upload', args.upload]
    if context.get('backup_dir'):
        options += ['--backup-dir', common.validate_backup(context['backup_dir'])]
    # 底层 API 使用相同的环境；密码不会写到阶段结果文件或日志。
    if 'SSHPASS' in connection.env:
        os.environ['SSHPASS'] = connection.env['SSHPASS']
    return main([*options, '--', *connection.ssh])


if __name__ == '__main__':
    try:
        raise SystemExit(entry())
    except (generator.AssociationError, common.DeployError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        common.message(f'[中止] {error}', 'error', file=sys.stderr)
        raise SystemExit(1)
