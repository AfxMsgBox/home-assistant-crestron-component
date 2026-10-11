#!/usr/bin/env python3
"""停止 HA Core 后，备份并清空设备注册表的 data.deleted_devices。"""

import getpass
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

import deploy_common as common
from list_ha_entities import Parser, clean, password_environment, use_color


REGISTRY = '/homeassistant/.storage/core.device_registry'

# 运行于 HA SSH 环境。只承认 Supervisor 明确的“未运行”结果，其他错误均停止。
# 依据 supervisor/2026.09.3 的 homeassistant/core.py 和 exceptions.py。
PRELUDE = r'''
set -eu
umask 077
registry=/homeassistant/.storage/core.device_registry
die() { printf '错误：%s\n' "$*" >&2; exit 1; }
for tool in jq ha timeout sha256sum cp mv mktemp cut cat date mkdir rmdir rm; do
    command -v "$tool" >/dev/null || die "服务器缺少 $tool"
done
valid_registry() {
    test ! -L "$registry" && test -f "$registry" || return 1
    jq -e '.key == "core.device_registry" and (.data | type == "object") and
      (.data.devices | type == "array") and
      ((.data | has("deleted_devices") | not) or (.data.deleted_devices | type == "array"))' "$registry" >/dev/null
}
file_hash() {
    checksum=$(sha256sum "$1") || return 1
    printf '%s' "$checksum" | cut -d ' ' -f 1
}
deleted_hash() {
    # 先完成 jq，避免管道掩盖解析失败。
    deleted=$(jq -cS '.data.deleted_devices // []' "$registry") || return 1
    printf '%s' "$deleted" | sha256sum | cut -d ' ' -f 1
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


def preview_script():
    return PRELUDE + r'''
valid_registry || die '设备注册表不存在、为符号链接或格式不正确'
count=$(jq '.data.deleted_devices // [] | length' "$registry")
digest=$(deleted_hash)
state=unchecked
if test "$count" != 0; then state=$(core_state); fi
jq -n --argjson count "$count" --arg digest "$digest" --arg state "$state" \
  '{count:$count,digest:$digest,state:$state}'
'''


def cleanup_script(preview):
    """把停止、改文件和异常恢复放在同一远端进程，缩小断线后的恢复窗口。"""
    if not isinstance(preview.get('digest'), str) or not re.fullmatch(r'[0-9a-f]{64}', preview['digest']) or preview.get('state') not in ('running', 'stopped'):
        raise common.DeployError('清理预览数据无效')
    return PRELUDE + '\nexpected=' + shlex.quote(preview['digest']) + '\noriginal_state=' + shlex.quote(preview['state']) + r'''
lock=/homeassistant/.crestron-device-cleanup.lock
mkdir "$lock" 2>/dev/null || die '已有清理任务或上次异常留下锁目录，请先核查'
stage=
backup=
old_hash=
new_hash=
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
finish() {
    code=$?
    trap - EXIT HUP INT TERM
    set +e
    if test "$changed" = yes && test "$committed" != yes; then
        recovery_safe=no
        # 已有第三方修改或 Core 重新启动时，不覆盖它正在使用的文件。
        if require_stopped && test ! -L "$registry"; then
            actual=$(file_hash "$registry")
            if test "$actual" = "$old_hash"; then
                recovery_safe=yes
            elif test "$actual" = "$new_hash" && test "$(file_hash "$backup")" = "$old_hash"; then
                if cp -p "$backup" "$stage/restore" && require_stopped &&
                   test "$(file_hash "$registry")" = "$new_hash" &&
                   mv "$stage/restore" "$registry" && test "$(file_hash "$registry")" = "$old_hash"; then
                    printf '校验失败，已恢复原设备注册表。\n' >&2
                    recovery_safe=yes
                fi
            fi
        fi
        if test "$recovery_safe" != yes; then
            printf '无法安全自动恢复，请检查 Core 状态和备份：%s\n' "$backup" >&2
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
valid_registry || die '设备注册表格式不正确'
test "$(deleted_hash)" = "$expected" || die '待删除记录在确认后发生变化，请重新预览'
test "$(core_state)" = "$original_state" || die 'Core 状态在确认后发生变化，请重新运行'
if test "$original_state" = running; then
    restart_needed=yes
    printf '正在停止 Home Assistant Core...\n'
    stop_reply=$(timeout 180 ha core stop --raw-json) || die 'Core 停止命令失败，未修改注册表'
    printf '%s' "$stop_reply" | jq -e '.result == "ok"' >/dev/null || die 'Core 停止请求未确认'
fi
require_stopped || die '无法确认 Core 已停止，未修改注册表'
valid_registry || die '停止后的设备注册表格式不正确'
# Core 停止时可能把内存数据落盘，必须重新检查用户确认的待删除集合。
test "$(deleted_hash)" = "$expected" || die '停止后的待删除记录发生变化，请重新预览'
count=$(jq '.data.deleted_devices | length' "$registry")
old_hash=$(file_hash "$registry")
stamp=$(date +%Y-%m-%d/%H%M%S)
parent="/homeassistant/crestron-backups/${stamp%/*}"
mkdir -p "$parent"
backup_dir=$(mktemp -d "$parent/${stamp#*/}-XXXXXX")
mkdir "$backup_dir/device-registry"
backup="$backup_dir/device-registry/core.device_registry"
cp -p "$registry" "$backup" || die '备份失败，未修改注册表'
test "$(file_hash "$backup")" = "$old_hash" || die '备份校验失败，未修改注册表'
printf '完整注册表备份：%s\n' "$backup"
stage=$(mktemp -d /homeassistant/.storage/.device-cleanup.XXXXXX)
cp -p "$registry" "$stage/new"
jq '.data.deleted_devices = []' "$backup" > "$stage/content"
# 写入复制文件以保留原文件的权限与所有者，然后进行同文件系统原子替换。
cat "$stage/content" > "$stage/new"
jq -e -s 'length == 2 and (.[1] == (.[0] | .data.deleted_devices = []))' "$backup" "$stage/new" >/dev/null \
    || die '生成结果校验失败，未修改注册表'
new_hash=$(file_hash "$stage/new")
require_stopped || die '写入前 Core 已重新启动'
test ! -L "$registry" && test "$(file_hash "$registry")" = "$old_hash" || die '注册表被其他进程修改，已取消覆盖'
changed=yes
mv "$stage/new" "$registry"
test "$(file_hash "$registry")" = "$new_hash" || die '写入后文件校验失败'
jq -e -s 'length == 2 and (.[1] == (.[0] | .data.deleted_devices = []))' "$backup" "$registry" >/dev/null \
    || die '写入后内容校验失败'
require_stopped || die '校验期间 Core 被其他进程启动，请检查备份'
committed=yes
printf '已清空 deleted_devices：%s 条。\n' "$count"
if test "$restart_needed" = yes; then
    start_core || die "文件已清理，但 Core 启动失败；备份：$backup"
else
    printf 'Core 原本已停止，保持停止状态。\n'
fi
'''


def parse_args(argv):
    parser = Parser(description=__doc__)
    parser.add_argument('--host', help='HA 服务器地址')
    parser.add_argument('--user', default='root', help='SSH 用户名，默认 root')
    parser.add_argument('--port', type=int, default=22, help='SSH 端口，默认 22')
    parser.add_argument('--password', default='', help='SSH 密码，省略或留空使用 key')
    parser.add_argument('--identity', help='私钥路径，省略时由 SSH 使用默认密钥和配置')
    parser.add_argument('--ha-url', default='', help='HA 网页地址，默认自动读取')
    parser.add_argument('--restart-timeout', type=int, default=120, help='启动后等待 HTTP 恢复秒数，默认 120')
    parser.add_argument('--color', choices=('auto', 'always', 'never'), default='auto', help='彩色输出，默认 auto')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='只读显示数量和状态，不停止或修改 HA')
    mode.add_argument('--yes', action='store_true', help='已确认清空全部 deleted_devices，跳过确认提示')
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
            preview = json.loads(connection.remote(preview_script(), capture=True))
            if not isinstance(preview, dict) or type(preview.get('count')) is not int or preview['count'] < 0:
                raise common.DeployError('服务器返回的待清理数量无效')
            say('HA 已删除设备记录清理', '1;36')
            print(f'注册表：{REGISTRY}\n待清理：{preview["count"]} 条', flush=True)
            if not preview['count']:
                say('没有需要清理的记录，无需停止 Core。', '32')
                return 0
            script = cleanup_script(preview)
            say('Core 当前状态：' + ('运行中' if preview['state'] == 'running' else '已停止'))
            if args.dry_run:
                say('预览结束，没有停止 Core 或修改服务器。', '32')
                return 0
            url = common.core_url(connection, args.ha_url) if preview['state'] == 'running' else ''
            if not args.yes and not common.ask_yes_no('将先停止 Core、备份并清空以上已删除设备记录，是否继续？'):
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
