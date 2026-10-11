#!/usr/bin/env python3
"""停止 HA Core 后，备份并清理已删除的设备或实体记录。"""

import getpass
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

import deploy_common as common
from list_ha_entities import Parser, clean, password_environment, use_color


REGISTRIES = {
    'devices': ('core.device_registry', 'devices', 'deleted_devices', '设备', 'device-registry'),
    'entities': ('core.entity_registry', 'entities', 'deleted_entities', '实体', 'entity-registry'),
}


def targets(target):
    if target not in ('devices', 'entities', 'all'):
        raise common.DeployError('清理范围必须为 devices、entities 或 all')
    return list(REGISTRIES) if target == 'all' else [target]


# 运行于 HA SSH 环境。仅使用固定注册表路径，所有范围共用原有排他锁。
PRELUDE = r'''
set -eu
umask 077
die() { printf '错误：%s\n' "$*" >&2; exit 1; }
for tool in jq ha timeout sha256sum cp mv mktemp cut cat date mkdir rmdir rm; do
    command -v "$tool" >/dev/null || die "服务器缺少 $tool"
done
select_registry() {
    kind=$1
    case "$kind" in
        devices) key=core.device_registry; active=devices; deleted=deleted_devices; label=设备; folder=device-registry ;;
        entities) key=core.entity_registry; active=entities; deleted=deleted_entities; label=实体; folder=entity-registry ;;
        *) die '未知清理范围' ;;
    esac
    registry="/homeassistant/.storage/$key"
}
valid_registry() {
    test ! -L "$registry" && test -f "$registry" || return 1
    jq -e --arg key "$key" --arg active "$active" --arg deleted "$deleted" '
      .key == $key and (.data | type == "object") and
      (.data[$active] | type == "array") and
      ((.data | has($deleted) | not) or (.data[$deleted] | type == "array"))' "$registry" >/dev/null
}
file_hash() {
    checksum=$(sha256sum "$1") || return 1
    printf '%s' "$checksum" | cut -d ' ' -f 1
}
deleted_hash() {
    # 先完成 jq，避免管道掩盖解析失败。
    deleted_json=$(jq -cS --arg field "$deleted" '.data[$field] // []' "${1:-$registry}") || return 1
    printf '%s' "$deleted_json" | sha256sum | cut -d ' ' -f 1
}
core_state() {
    status_code=0
    status_json=$(timeout 30 ha core stats --raw-json) || status_code=$?
    if test "$status_code" = 0 && printf '%s' "$status_json" | jq -e '.result == "ok" and (.data | type == "object")' >/dev/null 2>&1; then
        printf running
    elif { test "$status_code" = 0 || test "$status_code" = 1; } && printf '%s' "$status_json" | jq -e '.result == "error" and
      (.message == "Home Assistant is not running" or .error_key == "homeassistant_not_running_error")' >/dev/null 2>&1; then
        printf stopped
    else
        printf '无法确认 Core 状态；查询超时、权限或通信错误不视为停止。\n' >&2
        return 1
    fi
}
require_stopped() {
    current=$(core_state) || return 1
    test "$current" = stopped || { printf 'Core 仍在运行，禁止修改注册表。\n' >&2; return 1; }
}
'''


def selected_prelude(target):
    return PRELUDE + '\nselected=' + shlex.quote(' '.join(targets(target))) + '\n'


def preview_script(target='devices'):
    return selected_prelude(target) + '\ntarget=' + shlex.quote(target) + r'''
rows='[]'
for item in $selected; do
    select_registry "$item"
    valid_registry || die "$label 注册表不存在、为符号链接或格式不正确"
    count=$(jq --arg field "$deleted" '.data[$field] // [] | length' "$registry")
    digest=$(deleted_hash)
    rows=$(printf '%s' "$rows" | jq --arg target "$kind" --argjson count "$count" --arg digest "$digest" \
        '. + [{target:$target,count:$count,digest:$digest}]')
done
count=$(printf '%s' "$rows" | jq '[.[].count] | add')
state=unchecked
if test "$count" != 0; then state=$(core_state); fi
jq -n --arg target "$target" --argjson count "$count" --argjson registries "$rows" --arg state "$state" \
    '{target:$target,count:$count,registries:$registries,state:$state}'
'''


def validate_preview(preview, target):
    """确认服务器返回的范围、数量与摘要完整，拒绝漏掉一个注册表的结果。"""
    expected = targets(target)
    if not isinstance(preview, dict) or preview.get('target') != target:
        raise common.DeployError('清理预览范围无效')
    rows = preview.get('registries')
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise common.DeployError('清理预览注册表不完整')
    for row, kind in zip(rows, expected):
        if (not isinstance(row, dict) or row.get('target') != kind
                or type(row.get('count')) is not int or row['count'] < 0
                or not isinstance(row.get('digest'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', row['digest'])):
            raise common.DeployError('清理预览数量或摘要无效')
    count = sum(row['count'] for row in rows)
    if (type(preview.get('count')) is not int or preview['count'] != count
            or preview.get('state') not in (('running', 'stopped') if count else ('unchecked',))):
        raise common.DeployError('清理预览总数或 Core 状态无效')


def cleanup_script(preview):
    """两个文件先全部备份和生成，再逐个替换；异常恢复在同一远端进程中完成。"""
    target = preview.get('target') if isinstance(preview, dict) else None
    validate_preview(preview, target)
    if not preview['count']:
        raise common.DeployError('没有待清理记录')
    checks = '\n'.join(f"{row['target']}) printf '%s' '{row['digest']}' ;;" for row in preview['registries'])
    return (selected_prelude(target) + '\noriginal_state=' + shlex.quote(preview['state'])
            + '\nconfirmed_hash() { case "$kind" in\n' + checks + '\nesac; }\n') + r'''
lock=/homeassistant/.crestron-device-cleanup.lock
mkdir "$lock" 2>/dev/null || die '已有清理任务或上次异常留下锁目录，请先核查'
stage=
backup_dir=
changed=no
committed=no
restart_needed=no
start_attempted=no
recovery_safe=yes
start_core() {
    start_attempted=yes
    printf '正在恢复 Core 原来的运行状态...\n'
    start_reply=$(timeout 180 ha core start --raw-json) || return 1
    printf '%s' "$start_reply" | jq -e '.result == "ok"' >/dev/null || return 1
}
load_files() {
    backup="$backup_dir/$folder/$key"
    old_hash=$(cat "$stage/$kind/old_hash") || return 1
    new_hash=$(cat "$stage/$kind/new_hash") || return 1
}
recover_files() {
    require_stopped || return 1
    # 先检查整个批次，任一文件有外部修改都不盲目覆盖或自动启动。
    for item in $selected; do
        select_registry "$item"
        load_files || return 1
        test ! -L "$registry" && test -f "$registry" || return 1
        actual=$(file_hash "$registry") || return 1
        if test -f "$stage/$kind/changed"; then
            test "$actual" = "$old_hash" || test "$actual" = "$new_hash" || return 1
        else
            test "$actual" = "$old_hash" || return 1
        fi
        test "$(file_hash "$backup")" = "$old_hash" || return 1
    done
    for item in $selected; do
        select_registry "$item"
        test -f "$stage/$kind/changed" || continue
        load_files || return 1
        require_stopped || return 1
        test ! -L "$registry" || return 1
        actual=$(file_hash "$registry") || return 1
        test "$actual" != "$old_hash" || continue
        test "$actual" = "$new_hash" || return 1
        cp -p "$backup" "$stage/$kind/restore" || return 1
        require_stopped && test ! -L "$registry" && test "$(file_hash "$registry")" = "$new_hash" || return 1
        mv "$stage/$kind/restore" "$registry" || return 1
        test "$(file_hash "$registry")" = "$old_hash" || return 1
        printf '校验或写入失败，已恢复原%s注册表。\n' "$label" >&2
    done
}
finish() {
    code=$?
    trap - EXIT HUP INT TERM
    set +e
    if test "$changed" = yes && test "$committed" != yes; then
        recovery_safe=no
        if recover_files; then recovery_safe=yes; fi
        if test "$recovery_safe" != yes; then
            printf '无法安全自动恢复，请检查 Core 状态和备份：%s\n' "$backup_dir" >&2
            code=1
        fi
    fi
    if test "$restart_needed" = yes && test "$start_attempted" = no && test "$recovery_safe" = yes; then
        recovery_state=$(core_state)
        if test "$recovery_state" = stopped; then
            start_core || { printf 'Core 恢复启动失败，请手动检查。\n' >&2; code=1; }
        elif test "$recovery_state" != running; then
            printf 'Core 状态未知，请手动检查。\n' >&2
            code=1
        fi
    fi
    test -z "$stage" || rm -rf -- "$stage"
    rmdir "$lock"
    exit "$code"
}
trap finish EXIT
trap 'exit 1' HUP INT TERM
check_confirmed() {
    for item in $selected; do
        select_registry "$item"
        valid_registry || die "$label 注册表格式不正确"
        test "$(deleted_hash)" = "$(confirmed_hash)" || die "$label 待删除记录在确认后发生变化，请重新预览"
    done
}
check_confirmed
test "$(core_state)" = "$original_state" || die 'Core 状态在确认后发生变化，请重新运行'
if test "$original_state" = running; then
    restart_needed=yes
    printf '正在停止 Home Assistant Core...\n'
    stop_reply=$(timeout 180 ha core stop --raw-json) || die 'Core 停止命令失败，未修改注册表'
    printf '%s' "$stop_reply" | jq -e '.result == "ok"' >/dev/null || die 'Core 停止请求未确认'
fi
require_stopped || die '无法确认 Core 已停止，未修改注册表'
# Core 停止时会落盘，必须重新核对两个已确认的删除集合。
check_confirmed
stamp=$(date +%Y-%m-%d/%H%M%S)
parent="/homeassistant/crestron-backups/${stamp%/*}"
mkdir -p "$parent"
backup_dir=$(mktemp -d "$parent/${stamp#*/}-XXXXXX")
stage=$(mktemp -d /homeassistant/.storage/.device-cleanup.XXXXXX)
for item in $selected; do
    select_registry "$item"
    old_hash=$(file_hash "$registry")
    mkdir "$backup_dir/$folder" "$stage/$kind"
    backup="$backup_dir/$folder/$key"
    cp -p "$registry" "$backup" || die "$label 注册表备份失败，未修改注册表"
    test "$(file_hash "$backup")" = "$old_hash" || die "$label 注册表备份校验失败，未修改注册表"
    test "$(deleted_hash "$backup")" = "$(confirmed_hash)" || die "$label 备份中的待删除记录发生变化，请重新预览"
    printf '完整%s注册表备份：%s\n' "$label" "$backup"
    printf '%s' "$old_hash" > "$stage/$kind/old_hash"
    count=$(jq --arg field "$deleted" '.data[$field] // [] | length' "$backup")
    printf '%s' "$count" > "$stage/$kind/count"
    new_hash=$old_hash
    if test "$count" != 0; then
        cp -p "$registry" "$stage/$kind/new"
        jq --arg field "$deleted" '.data[$field] = []' "$backup" > "$stage/$kind/content"
        # 保留权限和所有者；逐文件同文件系统原子替换，不宣称跨文件原子性。
        cat "$stage/$kind/content" > "$stage/$kind/new"
        jq -e -s --arg field "$deleted" 'length == 2 and (.[1] == (.[0] | .data[$field] = []))' "$backup" "$stage/$kind/new" >/dev/null \
            || die "$label 注册表生成结果校验失败，未修改注册表"
        new_hash=$(file_hash "$stage/$kind/new")
    fi
    printf '%s' "$new_hash" > "$stage/$kind/new_hash"
done
# 所有备份及新文件就绪，才开始写入。
for item in $selected; do
    select_registry "$item"
    load_files
    require_stopped || die '写入前 Core 已重新启动'
    test ! -L "$registry" && test "$(file_hash "$registry")" = "$old_hash" || die "$label 注册表被其他进程修改，已取消覆盖"
    test -f "$stage/$kind/new" || continue
    : > "$stage/$kind/changed"
    changed=yes
    mv "$stage/$kind/new" "$registry"
    test "$(file_hash "$registry")" = "$new_hash" || die "$label 注册表写入后文件校验失败"
    jq -e -s --arg field "$deleted" 'length == 2 and (.[1] == (.[0] | .data[$field] = []))' "$backup" "$registry" >/dev/null \
        || die "$label 注册表写入后内容校验失败"
done
for item in $selected; do
    select_registry "$item"
    load_files
    test ! -L "$registry" && test "$(file_hash "$registry")" = "$new_hash" || die "$label 注册表在最终校验时发生变化"
done
require_stopped || die '校验期间 Core 被其他进程启动，请检查备份'
committed=yes
for item in $selected; do
    select_registry "$item"
    printf '已清空 %s：%s 条。\n' "$deleted" "$(cat "$stage/$kind/count")"
done
if test "$restart_needed" = yes; then
    start_core || die "文件已清理，但 Core 启动失败；备份：$backup_dir"
else
    printf 'Core 原本已停止，保持停止状态。\n'
fi
'''


def parse_args(argv):
    parser = Parser(description=__doc__)
    parser.add_argument('--host', help='HA 服务器地址')
    parser.add_argument('--target', choices=('devices', 'entities', 'all'), default='devices',
                        help='清理设备、实体或两者，默认 devices（保持原用法）')
    parser.add_argument('--user', default='root', help='SSH 用户名，默认 root')
    parser.add_argument('--port', type=int, default=22, help='SSH 端口，默认 22')
    parser.add_argument('--password', default='', help='SSH 密码，省略或留空使用 key')
    parser.add_argument('--identity', help='私钥路径，省略时由 SSH 使用默认密钥和配置')
    parser.add_argument('--ha-url', default='', help='HA 网页地址，默认自动读取')
    parser.add_argument('--restart-timeout', type=int, default=120, help='启动后等待 HTTP 恢复秒数，默认 120')
    parser.add_argument('--color', choices=('auto', 'always', 'never'), default='auto', help='彩色输出，默认 auto')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='只读显示数量和状态，不停止或修改 HA')
    mode.add_argument('--yes', action='store_true', help='已确认清空所选范围的全部已删除记录，跳过确认提示')
    args = parser.parse_args(argv)
    if not argv:
        args.host = input('HA 服务器地址：').strip()
        args.user = input('SSH 用户名 [root]：').strip() or 'root'
        while True:
            port = input('SSH 端口 [22]：').strip() or '22'
            if port.isascii() and port.isdigit() and 1 <= int(port) <= 65535:
                args.port = int(port)
                break
            print('请输入 1..65535 之间的端口。')
        args.password = getpass.getpass('SSH 密码（留空使用 SSH key）：')
        if not args.password:
            args.identity = input('私钥路径（留空使用 ~/.ssh/ 默认密钥和配置）：').strip() or None
        while True:
            choice = input('清理范围 [1 设备（默认）/ 2 实体 / 3 两者]：').strip() or '1'
            if choice in ('1', '2', '3'):
                args.target = {'1': 'devices', '2': 'entities', '3': 'all'}[choice]
                break
            print('请输入 1、2 或 3。')
    if not args.host:
        parser.error('请指定 --host，或不带参数逐步输入')
    if not 1 <= args.port <= 65535 or not 1 <= args.restart_timeout <= 9999:
        parser.error('端口须在 1..65535，等待时间须在 1..9999 秒')
    if args.identity:
        args.identity = str(Path(args.identity).expanduser())
    return args


def main(argv=None):
    try:
        args = parse_args(list(sys.argv[1:] if argv is None else argv))
        color = use_color(args.color, sys.stdout)

        def say(text, code='36'):
            print(f'\033[{code}m{text}\033[0m' if color else text, flush=True)

        with password_environment(args.password):
            connection = common.Connection(args)
            preview = json.loads(connection.remote(preview_script(args.target), capture=True))
            validate_preview(preview, args.target)
            say('HA 已删除设备/实体记录清理', '1;36')
            for row in preview['registries']:
                key, _, field, label, _ = REGISTRIES[row['target']]
                print(f'{label}注册表：/homeassistant/.storage/{key}\n待清理 {field}：{row["count"]} 条', flush=True)
            if not preview['count']:
                say('没有需要清理的记录，无需停止 Core。', '32')
                return 0
            script = cleanup_script(preview)
            say('Core 当前状态：' + ('运行中' if preview['state'] == 'running' else '已停止'))
            if args.dry_run:
                say('预览结束，没有停止 Core 或修改服务器。', '32')
                return 0
            url = common.core_url(connection, args.ha_url) if preview['state'] == 'running' else ''
            if not args.yes and not common.ask_yes_no('将先停止 Core、备份并清空以上所选的已删除记录，是否继续？'):
                say('已取消，服务器未修改。')
                return 0
            # 无本机超时强杀：每个远端 HA 命令单独限时，异常由远端 trap 恢复。
            # SSH 断线/强制终止仍需核查现场状态，不自动重试具有写入效果的命令。
            connection.remote(script, timeout=None, message='清理未完整完成，请检查上方恢复提示和服务器备份，勿盲目重复运行')
            if url:
                common.wait_for_http(connection, url, args.restart_timeout)
            say('清理完成，备份已保存在 HA 服务器。', '1;32')
            return 0
    except (EOFError, KeyboardInterrupt):
        print('\n操作已中断；若已经开始清理，请核查 Core 状态及服务器备份。', file=sys.stderr)
        return 130
    except subprocess.TimeoutExpired:
        print('错误：查询或恢复等待超时，请核查 Core 状态和服务器备份。', file=sys.stderr)
        return 1
    except (common.DeployError, OSError, ValueError, KeyError) as error:
        print(f'错误：{clean(error)}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
