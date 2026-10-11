#!/usr/bin/env python3
"""只读查询 HA 实体：多个关键词按“或”匹配，显示位置、名称及完整 ID。"""

import argparse
from contextlib import contextmanager
import csv
import getpass
import io
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import unicodedata

import deploy_common as common
import deploy_automations as automation


HEADERS = ('楼层', '房间', '设备名称', '实体名称', 'entity_id', 'device_id')
DEFAULT_KEYWORDS = ('_occupancy_status_p_', '_oh4w_on_p_')


def query_keywords(values):
    """空输入使用默认筛选；单独的星号表示全部，其余按字面匹配。"""
    if values == ['*']:
        return []
    return list(dict.fromkeys(value.casefold() for value in (values or DEFAULT_KEYWORDS)))


def prompt_keywords():
    return query_keywords(input('关键词（空格分隔；回车用默认值，* 查全部）：').split())


class Parser(argparse.ArgumentParser):
    def __init__(self, **kwargs):
        super().__init__(add_help=False, **kwargs)
        self._positionals.title = '关键词'
        self._optionals.title = '选项'
        self.add_argument('-h', '--help', action='help', help='显示帮助并退出')

    def format_usage(self):
        return super().format_usage().replace('usage:', '用法：', 1)

    def format_help(self):
        return super().format_help().replace('usage:', '用法：', 1)

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f'参数错误：{message}\n')


def parse_args(argv):
    parser = Parser(description=__doc__, epilog='不带参数时连续查询，Ctrl+C 退出；默认关键词：' + ' '.join(DEFAULT_KEYWORDS))
    parser.add_argument('keywords', nargs='*', help='entity_id 包含任意关键词即可；省略用默认值，单独传入 "*" 查全部')
    parser.add_argument('--host', help='HA 服务器地址或 SSH 配置中的别名')
    parser.add_argument('--user', default='root', help='SSH 用户名，默认 root')
    parser.add_argument('--port', type=int, default=22, help='SSH 端口，默认 22')
    parser.add_argument('--password', default='', help='SSH 密码；省略或留空使用 SSH key')
    parser.add_argument('--identity', help='私钥文件；省略时遵循 SSH 的默认密钥及 ~/.ssh/config')
    parser.add_argument('--ha-url', default='', help='HA HTTP 地址；默认通过服务器 ha core info 获取')
    parser.add_argument('--color', choices=('auto', 'always', 'never'), default='auto',
                        help='颜色：auto 自动（默认）、always 总是、never 关闭')
    parser.add_argument('--format', choices=('table', 'tsv'), default='table',
                        help='输出格式：table 对齐表格（默认）、tsv 制表符分隔')
    args = parser.parse_args(argv)
    args.interactive = not argv
    if not argv:
        args.host = input('HA 服务器地址：').strip()
        while not args.host:
            args.host = input('地址不能为空，请输入 HA 服务器地址：').strip()
        args.user = input('SSH 用户名 [root]：').strip() or 'root'
        while True:
            port = input('SSH 端口 [22]：').strip() or '22'
            if port.isascii() and port.isdigit() and 1 <= int(port) <= 65535:
                args.port = int(port)
                break
            print('请输入 1..65535 之间的端口。', file=sys.stderr)
        args.password = getpass.getpass('SSH 密码（留空使用 SSH key）：')
        if not args.password:
            args.identity = input('私钥路径（留空使用 ~/.ssh/ 默认密钥和配置）：').strip() or None
        print('默认关键词：' + ' '.join(DEFAULT_KEYWORDS) + '；Ctrl+C 退出。', file=sys.stderr)
        args.keywords = input('关键词（空格分隔；回车用默认值，* 查全部）：').split()
    if not args.host:
        parser.error('请使用 --host 指定 HA 地址，或不带参数进入交互模式')
    if not 1 <= args.port <= 65535:
        parser.error('--port 必须在 1..65535 之间')
    if any(not value.strip() for value in args.keywords):
        parser.error('关键词不能为空字符串；显示全部时单独传入 "*"')
    args.keywords = query_keywords(args.keywords)
    if args.identity:
        args.identity = str(Path(args.identity).expanduser())
    return args


@contextmanager
def password_environment(password):
    """不继承部署密码；本工具省略 --password 就使用 key，结束后恢复环境。"""
    names = ('HA_SSH_PASSWORD', 'SSHPASS')
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ[name] = password
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def fetch_snapshot(args):
    # 只调用读取方法。所有 API 请求为读取状态或获取认证令牌，不调用设备服务。
    with password_environment(args.password):
        connection = common.Connection(args)
        connection.remote('command -v jq >/dev/null 2>&1', message='服务器需要 jq 来读取注册信息')
        url = common.core_url(connection, args.ha_url)
        return automation.read_registry(connection.ssh, automation.Api(connection.ssh, url))


def indexed(snapshot, key, field):
    values = snapshot.get(key, [])
    if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
        raise common.DeployError(f'HA 返回的 {key} 数据格式不正确')
    return {value[field]: value for value in values if isinstance(value.get(field), str)}


def entity_rows(snapshot, keywords):
    if not isinstance(snapshot, dict):
        raise common.DeployError('HA 返回的注册信息格式不正确')
    entities = indexed(snapshot, 'entities', 'entity_id')
    states = indexed(snapshot, 'states', 'entity_id')
    devices = indexed(snapshot, 'devices', 'id')
    areas = indexed(snapshot, 'areas', 'id')
    floors = indexed(snapshot, 'floors', 'id')
    terms = [value.casefold() for value in keywords]
    rows = []
    # 注册表包含停用/尚未加载的实体，实时状态补充没有 unique_id 的实体。
    for entity_id in entities.keys() | states.keys():
        if terms and not any(term in entity_id.casefold() for term in terms):
            continue
        entity = entities.get(entity_id, {})
        state = states.get(entity_id, {})
        attributes = state.get('attributes') or {}
        device_id = entity.get('device_id')
        device = devices.get(device_id, {})
        device_name = device.get('name_by_user') or device.get('name')
        area = areas.get(entity.get('area_id') or device.get('area_id'), {})
        floor = floors.get(area.get('floor_id'), {})
        entity_name = entity.get('name') or attributes.get('friendly_name')
        if not entity_name:
            entity_name = entity.get('original_name') or ''
            if entity.get('has_entity_name') and device_name:
                entity_name = ' '.join(filter(None, (device_name, entity_name)))
        # 仅简化查询结果，不修改 HA 名称；相同设备名称按完整字面文本移除。
        if device_name and entity_name and device_name in entity_name:
            entity_name = entity_name.replace(device_name, '').strip(' \t\r\n-_/／:：·') or '—'
        rows.append((floor.get('name') or '未分配', area.get('name') or '未分配',
                     device_name or ('设备信息缺失' if device_id else '无关联设备'),
                     entity_name or '未命名', entity_id, device_id or '—'))
    return sorted(rows, key=lambda row: tuple(str(value).casefold() for value in row))


def clean(value):
    """控制字符转为可见文本，防止名称中的换行或转义序列破坏表格。"""
    result = []
    for char in str(value):
        if char == '\n':
            result.append(r'\n')
        elif char == '\r':
            result.append(r'\r')
        elif char == '\t':
            result.append(r'\t')
        elif unicodedata.category(char).startswith('C'):
            result.append(f'\\u{ord(char):04x}')
        else:
            result.append(char)
    return ''.join(result)


def display_width(text):
    # 常规中文/全角字符占两列；组合附加符号不额外占列。
    return sum(0 if unicodedata.category(char) in ('Mn', 'Me') else
               2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1 for char in text)


def use_color(mode, stream):
    if mode != 'auto':
        return mode == 'always'
    return stream.isatty() and os.environ.get('TERM') != 'dumb' and 'NO_COLOR' not in os.environ


def render(rows, keywords, *, stream, color='auto', format='table'):
    values = [tuple(clean(value) for value in row) for row in rows]
    if format == 'tsv':
        writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
        writer.writerow(HEADERS)
        writer.writerows(values)
        return
    enabled = use_color(color, stream)

    def paint(text, code):
        return f'\033[{code}m{text}\033[0m' if enabled else text

    widths = [max(display_width(row[i]) for row in [HEADERS, *values]) for i in range(len(HEADERS))]
    matching = re.compile('|'.join(re.escape(term) for term in keywords), re.IGNORECASE) if keywords else None

    def border(left, middle, right):
        print(paint(left + middle.join('─' * (width + 2) for width in widths) + right, '2'), file=stream)

    def line(row, header=False):
        cells = []
        for index, value in enumerate(row):
            padding = ' ' * (widths[index] - display_width(value))
            if header:
                value = paint(value, '1;36')
            elif index == 4:
                if matching and enabled:
                    value = matching.sub(lambda match: paint(match.group(), '1;33'), value)
                else:
                    value = paint(value, '32')
            elif index == 5 or value in ('未分配', '无关联设备', '未命名', '—'):
                value = paint(value, '2')
            elif index < 2:
                value = paint(value, '36')
            cells.append(' ' + value + padding + ' ')
        print('│' + '│'.join(cells) + '│', file=stream)

    print(paint(f'HA 实体查询 · 匹配 {len(rows)} 个实体', '1;36'), file=stream)
    print('筛选：' + (' / '.join(clean(term) for term in keywords) + '（任意包含）' if keywords else '全部'), file=stream)
    if not rows:
        print(paint('没有匹配的实体。', '33'), file=stream)
        return
    border('┌', '┬', '┐')
    line(HEADERS, header=True)
    border('├', '┼', '┤')
    for row in values:
        line(row)
    border('└', '┴', '┘')


def show_results(rows, args):
    """完整表格超出终端时由 less 横向浏览，不插入换行或省略 ID。"""
    if args.format == 'tsv' or not sys.stdout.isatty():
        render(rows, args.keywords, stream=sys.stdout, color=args.color, format=args.format)
        return
    output = io.StringIO()
    color = 'always' if use_color(args.color, sys.stdout) else 'never'
    render(rows, args.keywords, stream=output, color=color)
    text = output.getvalue()
    plain = re.sub(r'\x1b\[[0-9;]*m', '', text)
    size = shutil.get_terminal_size()
    lines = plain.splitlines()
    if max(map(display_width, lines), default=0) < size.columns and len(lines) < size.lines - 2:
        print(text, end='', flush=True)
        return
    less = shutil.which('less')
    if not less:
        raise common.DeployError('表格超出终端宽度或高度，请在本机安装 less；也可用 --format tsv 导出完整结果')
    print('方向键浏览（左右横移），q 返回，Ctrl+C 退出。', file=sys.stderr, flush=True)
    # 不继承 LESS 中的自动退出/折行设置；禁用外部预处理器和 less 的写入命令。
    env = dict(os.environ, LESS='', LESSSECURE='1', LESSCHARSET='utf-8')
    for name in ('LESSOPEN', 'LESSCLOSE'):
        env.pop(name, None)
    with subprocess.Popen([less, '-S', '-R', '-K'], stdin=subprocess.PIPE, text=True,
                          encoding='utf-8', env=env) as pager:
        try:
            pager.communicate(text)
        except KeyboardInterrupt:
            # 让 less 恢复终端模式后再退出，避免直接强杀留下无回显的终端。
            if pager.poll() is None:
                pager.send_signal(signal.SIGINT)
                try:
                    pager.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pager.kill()
                    pager.wait()
            raise
        if pager.returncode in (2, -signal.SIGINT):
            raise KeyboardInterrupt
        if pager.returncode:
            raise common.DeployError(f'表格查看器退出异常（{pager.returncode}）')


def main(argv=None):
    try:
        args = parse_args(list(sys.argv[1:] if argv is None else argv))
        while True:
            try:
                print('正在只读查询 HA 实体和位置...', file=sys.stderr, flush=True)
                rows = entity_rows(fetch_snapshot(args), args.keywords)
                show_results(rows, args)
            except subprocess.TimeoutExpired:
                print('错误：查询超时，请检查 SSH 和 HA 连接。', file=sys.stderr)
                if not args.interactive:
                    return 1
            except (common.DeployError, automation.generator.AssociationError, OSError, ValueError, KeyError) as error:
                print(f'错误：{clean(error)}', file=sys.stderr)
                if not args.interactive:
                    return 1
            if not args.interactive:
                return 0
            args.keywords = prompt_keywords()
    except (EOFError, KeyboardInterrupt):
        print('\n已退出查询。', file=sys.stderr)
        return 130
    except subprocess.TimeoutExpired:
        print('错误：查询超时，请检查 SSH 和 HA 连接。', file=sys.stderr)
        return 1
    except (common.DeployError, automation.generator.AssociationError, OSError, ValueError, KeyError) as error:
        print(f'错误：{clean(error)}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
