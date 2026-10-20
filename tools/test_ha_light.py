#!/usr/bin/env python3
"""现场观察单盏灯：倒计时后开灯、等待、关灯，打印 HA 反馈，最后保持关闭。"""

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time

import deploy_common as common
from deploy_automations import Api, remote


def log(message):
    print(f'{datetime.now().astimezone():%H:%M:%S}  {message}', flush=True)


def show_state(api, entity_id):
    state = api.request('api/states/' + entity_id)
    attrs = state.get('attributes', {})
    brightness = attrs.get('brightness')
    memory = attrs.get('last_nonzero_brightness')
    display = '无反馈' if brightness is None else f'{brightness / 255:.1%}'
    remembered = '无记录' if memory is None else f'{memory / 65535:.1%}（原值 {memory}）'
    log(f'HA 状态：{state.get("state")}；当前亮度：{display}；记忆亮度：{remembered}')
    return state


def service(api, entity_id, action):
    # 固定 light 服务及单个实体，不接受组目标，不向服务附加亮度或模式。
    if action not in ('turn_on', 'turn_off'):
        raise ValueError('仅支持开灯、关灯')
    config = [
        'url = ' + json.dumps(api.url + 'api/services/light/' + action),
        'request = "POST"',
        'header = ' + json.dumps('Authorization: Bearer ' + api.token),
        'header = "Content-Type: application/json"',
        'data = ' + json.dumps(json.dumps({'entity_id': entity_id})),
    ]
    log('发送：' + ('开灯（恢复记忆亮度）' if action == 'turn_on' else '关灯'))
    # 认证及请求 JSON 只经 SSH 标准输入，不进入命令行参数或临时文件。
    remote(api.command, 'curl -fsS --connect-timeout 5 --max-time 30 --config -',
           ('\n'.join(config) + '\n').encode())


def observe(api, entity_id, seconds):
    deadline = time.monotonic() + seconds
    while True:
        state = show_state(api, entity_id)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return state
        time.sleep(min(1, remaining))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='HA 服务器地址')
    parser.add_argument('--entity', required=True, help='单盏灯的完整 light 实体 ID')
    parser.add_argument('--user', default='root', help='SSH 用户名，默认 root')
    parser.add_argument('--port', type=int, default=22, help='SSH 端口，默认 22')
    parser.add_argument('--identity', help='私钥路径；省略使用 ~/.ssh/ 默认密钥及配置')
    parser.add_argument('--password', default='', help='SSH 密码；省略使用密钥')
    parser.add_argument('--ha-url', default='', help='HA HTTP 地址，默认自动获取')
    parser.add_argument('--countdown', type=float, default=5, help='开灯前倒计时秒数，默认 5')
    parser.add_argument('--interval', type=float, default=5, help='开灯后观察秒数，默认 5')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'light\.[a-z0-9_]+', args.entity):
        parser.error('--entity 必须是单个完整 light 实体 ID')
    for name in ('countdown', 'interval'):
        value = getattr(args, name)
        if not math.isfinite(value) or not 0 <= value <= 60:
            parser.error(f'--{name} 必须在 0 到 60 秒之间')
    if args.identity:
        args.identity = str(Path(args.identity).expanduser())
    os.environ['HA_SSH_PASSWORD'] = args.password
    os.environ['SSHPASS'] = args.password
    connection = common.Connection(args)
    api = Api(connection.ssh, common.core_url(connection, args.ha_url))
    log(f'目标：{args.entity}；使用服务器现有集成；结束后保持关闭')
    initial = show_state(api, args.entity)
    if initial.get('state') not in ('on', 'off'):
        raise common.DeployError('实体状态未知或不可用，未发送控制命令')
    if initial['state'] == 'on':
        log('灯当前开启，先关灯以便观察重新开启')
        service(api, args.entity, 'turn_off')
        if observe(api, args.entity, 2).get('state') != 'off':
            raise common.DeployError('未确认关灯，停止开灯测试')
    log(f'{args.countdown:g} 秒后开灯，观察 {args.interval:g} 秒，然后关灯')
    time.sleep(args.countdown)
    opened = False
    try:
        service(api, args.entity, 'turn_on')
        opened = observe(api, args.entity, args.interval).get('state') == 'on'
    finally:
        # 开灯失败或观察中断也尝试关灯，不额外调整亮度来掩盖失败。
        service(api, args.entity, 'turn_off')
        closed = observe(api, args.entity, 2).get('state') == 'off'
    log(f'结果：开启反馈{"已确认" if opened else "未确认"}；关闭反馈{"已确认" if closed else "未确认"}')
    log('HA 反馈不能代替实物观察，请记录是否亮起、是否先全亮再变暗。')
    return 0 if opened and closed else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log('测试已中断，请核对灯具最终状态')
        raise SystemExit(130)
    except (common.DeployError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        log(f'测试失败：{exc}')
        raise SystemExit(1)
