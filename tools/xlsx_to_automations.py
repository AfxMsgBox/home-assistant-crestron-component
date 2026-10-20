#!/usr/bin/env python3
"""根据 .xlsx 关联列、HA 注册表和模板，独立生成 automations_manual.yaml。"""

import argparse
import json
import os
from pathlib import Path
import re
import tempfile

import xlsx_to_yaml as devices

SWITCH_COLUMN = 'HA关联开关实体ID'
OCCUPANCY_COLUMN = 'HA关联人在实体ID'
DEFAULT_TEMPLATE = Path(__file__).resolve().parents[1] / 'automations_manual_template.yaml'
ENTITY_RE = re.compile(r'^[a-z0-9_]+\.[a-z0-9_]+$')


class AssociationError(ValueError):
    """配置无效，不得覆盖已有自动化。"""


class RegistryPending(AssociationError):
    """相关实体尚未注册/加载，部署工具可限时重试。"""


def split_ids(value, domain, location):
    result = []
    for part in str(value or '').replace('\\n', '\n').splitlines():
        entity = part.strip()
        if not entity:
            continue
        if not ENTITY_RE.fullmatch(entity) or not entity.startswith(domain + '.'):
            raise AssociationError(f'{location}：{entity!r} 必须是 {domain}.* 实体 ID，每行一项')
        if entity not in result:
            result.append(entity)
    return result


def identity(platform, config):
    """与集成 unique_ids.py 的 Excel 支持平台保持一致；有契约测试。"""
    if platform == 'light':
        if 'brightness_join' in config:
            return f"crestron_light_{config['brightness_join']}"
        return f"crestron_light_onoff_d{config['on_join']}"
    if platform == 'switch':
        return f"crestron_switch_d{config['on_join']}"
    if platform == 'climate':
        return f"crestron_climate_d{config['on_join']}"
    raise AssociationError(f'不支持 {platform} 自动化关联')


def read_associations(xlsx):
    by_platform, errors, _, messages = devices._build_from_workbook(str(xlsx))
    if errors:
        raise AssociationError('\n'.join(m for m in messages if m.startswith('[错误]')))
    configs = {}
    for platform in ('light', 'switch', 'climate'):
        for cfg in by_platform.get(platform, []):
            configs.setdefault((platform, identity(platform, cfg)), []).append(cfg)
    rows = []
    for sheet, data in devices.parse_xlsx(str(xlsx)).items():
        for row in data:
            location = f"{sheet}表第{row['_xlsx_row']}行"
            switches = split_ids(row.get(SWITCH_COLUMN), 'switch', location + '开关关联')
            sensors = split_ids(row.get(OCCUPANCY_COLUMN), 'binary_sensor', location + '人在关联')
            if not switches and not sensors:
                continue
            if sheet not in ('灯光', '插座', '空调'):
                raise AssociationError(f'{location}：仅灯光、插座、空调支持开关关联')
            if sensors and sheet == '空调':
                raise AssociationError(f'{location}：空调暂不支持人在关联')
            result = devices.SHEET_BUILDERS[sheet](row)
            if result is None or devices._placeholder_reason(sheet, row) is not None:
                raise AssociationError(f'{location}：已填关联但没有有效设备配置')
            platform, raw = result
            uid = identity(platform, raw)
            candidates = configs.get((platform, uid), [])
            if len(candidates) != 1:
                raise AssociationError(f'{location}：设备唯一标识 {uid} 在 Excel 中重复或无效')
            rows.append(dict(location=location, platform=platform, unique_id=uid,
                             config=candidates[0], switches=switches, sensors=sensors))
    return rows


class Registry:
    """注册表快照中只使用实体、设备、区域、楼层及可选实时状态。"""

    def __init__(self, snapshot):
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('entities'), list):
            raise AssociationError('注册表文件必须包含 entities 列表；请使用部署工具导出')
        for key in ('entities', 'devices', 'areas', 'floors', 'states'):
            if key in snapshot and (not isinstance(snapshot[key], list)
                                    or not all(isinstance(x, dict) for x in snapshot[key])):
                raise AssociationError(f'注册表 {key} 必须为对象列表')
        self.entities = {}
        self.identities = {}
        for entity in snapshot['entities']:
            eid = entity.get('entity_id', '')
            if not isinstance(eid, str) or not ENTITY_RE.fullmatch(eid):
                raise AssociationError('注册表包含无效 entity_id')
            if eid in self.entities:
                raise AssociationError(f'注册表实体 ID 重复：{eid}')
            self.entities[eid] = entity
            if entity.get('platform') == 'crestron':
                key = (eid.split('.')[0], entity.get('unique_id'))
                self.identities.setdefault(key, []).append(entity)
        self.devices = {d['id']: d for d in snapshot.get('devices', [])}
        self.areas = {a['id']: a for a in snapshot.get('areas', [])}
        self.floors = {f.get('id', f.get('floor_id')): f for f in snapshot.get('floors', [])}
        self.states = {s['entity_id']: s for s in snapshot.get('states', [])}
        self.has_states = 'states' in snapshot

    def check_entity(self, eid, location):
        entry = self.entities.get(eid)
        if entry is None and eid in self.states:
            entry = {'entity_id': eid}
        if entry is None:
            raise RegistryPending(f'{location}：HA 尚未找到实体 {eid}')
        if entry.get('disabled_by') is not None:
            raise AssociationError(f'{location}：实体 {eid} 已禁用，请先在 HA 启用')
        if self.has_states and eid not in self.states:
            raise RegistryPending(f'{location}：实体 {eid} 已注册但尚未加载到 HA 状态中')
        return entry

    def target(self, row):
        candidates = self.identities.get((row['platform'], row['unique_id']), [])
        if not candidates:
            raise RegistryPending(f"{row['location']}：{row['config']['name']} 尚未注册（{row['unique_id']}）")
        if len(candidates) != 1:
            raise AssociationError(f"{row['location']}：HA 中有多个实体匹配 {row['unique_id']}")
        eid = candidates[0]['entity_id']
        self.check_entity(eid, row['location'])
        return eid

    def describe(self, eid):
        """返回独立的位置和名称字段，供文本报告及部署表格共用。"""
        entry = self.entities.get(eid, {})
        device = self.devices.get(entry.get('device_id'), {})
        area_id = entry.get('area_id') or device.get('area_id')
        area = self.areas.get(area_id, {})
        floor = self.floors.get(area.get('floor_id'), {})
        attrs = self.states.get(eid, {}).get('attributes', {})
        name = (entry.get('name') or attrs.get('friendly_name') or entry.get('original_name')
                or device.get('name_by_user') or device.get('name') or eid)
        if entry.get('has_entity_name') and not attrs.get('friendly_name'):
            prefix = device.get('name_by_user') or device.get('name')
            if prefix and prefix != name:
                name = f'{prefix} {name}'
        name = ' '.join(str(name).split())
        return floor.get('name') or '未分配楼层', area.get('name') or '未分配房间', name

    def label(self, eid):
        return ' / '.join(self.describe(eid)) + f' [{eid}]'


def build_maps(rows, snapshot):
    registry = Registry(snapshot)
    switch_map, occupancy_map, report, missing = {}, {}, [], []
    target_labels = {}
    for row in rows:
        try:
            target = registry.target(row)
            for source in row['switches'] + row['sensors']:
                registry.check_entity(source, row['location'])
        except RegistryPending as error:
            missing.append(str(error))
            continue
        cfg = row['config']
        target_label = f"{cfg['floor']} / {cfg['room']} / {cfg['name']} [{target}]"
        target_labels[target] = target_label
        key = row['platform'] + '_entity_ids'
        for field, mapping, kind in [('switches', switch_map, '开关双向关联'),
                                      ('sensors', occupancy_map, '人在控制')]:
            for source in row[field]:
                lists = mapping.setdefault(source, {})
                targets = lists.setdefault(key, [])
                if target not in targets:
                    targets.append(target)
                    report.append(f"{row['location']}：{target_label} ← {kind} — {registry.label(source)}")
    if missing:
        raise RegistryPending('\n'.join(missing))
    # 控制端与被控端禁止重叠：反馈本身会反向写开关，不能只检查单向有向环。
    sources = set(switch_map)
    targets = {e for m in (switch_map, occupancy_map) for cfg in m.values()
               for values in cfg.values() for e in values}
    overlap = sorted(sources & targets)
    if overlap:
        raise AssociationError('控制开关不能同时作为被控设备（会产生反馈循环）：' + '、'.join(overlap))
    for mapping in (switch_map, occupancy_map):
        for cfg in mapping.values():
            for values in cfg.values():
                values.sort()
    for source, cfg in sorted(switch_map.items()):
        n = sum(len(v) for v in cfg.values())
        report.append(f'开关组：{registry.label(source)}，关联 {n} 台设备；任意开启算开，全部关闭才算关。')
    for target in sorted({e for cfg in occupancy_map.values() for v in cfg.values() for e in v}):
        sensors = [s for s, cfg in occupancy_map.items() if any(target in v for v in cfg.values())]
        report.append(f'人在组：{target_labels[target]}，关联 {len(sensors)} 个传感器；任意有人开启，全部无人 30 秒关闭。')
    report.append(f'合计：{len(switch_map)} 个控制开关、{len(occupancy_map)} 个人在传感器、{len(targets)} 台被控设备。')
    if not targets:
        report.append('未填写有效关联；成品为空触发器自动化，部署覆盖后会移除该文件中的旧关联。')
    return switch_map, occupancy_map, report


def yaml_lines(value, indent=0):
    """生成本工具所需的简单 YAML 类型，始终引用字符串，避免 on/off 变布尔。"""
    pad = ' ' * indent
    if isinstance(value, dict) and value:
        for key, item in value.items():
            key = json.dumps(key, ensure_ascii=False)
            if isinstance(item, (dict, list)) and item:
                yield f'{pad}{key}:'
                yield from yaml_lines(item, indent + 2)
            else:
                yield f'{pad}{key}: {json.dumps(item, ensure_ascii=False)}'
    elif isinstance(value, list) and value:
        for item in value:
            if isinstance(item, (dict, list)) and item:
                yield f'{pad}-'
                yield from yaml_lines(item, indent + 2)
            else:
                yield f'{pad}- {json.dumps(item, ensure_ascii=False)}'
    else:
        yield pad + json.dumps(value, ensure_ascii=False)


def render_template(template, switch_map, occupancy_map, report):
    feedback = sorted({e for cfg in switch_map.values() for v in cfg.values() for e in v})
    triggers = []
    for tid, entities, state in [('switch_changed', sorted(switch_map), None),
                                 ('crestron_changed', feedback, None),
                                 ('occupancy_on', sorted(occupancy_map), 'on'),
                                 ('occupancy_off', sorted(occupancy_map), 'off')]:
        if not entities:
            continue
        trigger = dict(trigger='state', entity_id=entities, to=state, id=tid)
        if tid == 'occupancy_off':
            trigger['for'] = '00:00:30'
        triggers.append(trigger)
    text = template
    for name, value in [('TRIGGERS', triggers), ('SWITCH_MAP', switch_map), ('OCCUPANCY_MAP', occupancy_map)]:
        pattern = rf'(?m)^( *)@@{name}@@$'
        matches = list(re.finditer(pattern, text))
        if len(matches) != 1:
            raise AssociationError(f'模板标记 {name} 必须独占一行且只出现一次')
        replacement = '\n'.join(yaml_lines(value, len(matches[0][1])))
        text = re.sub(pattern, lambda _: replacement, text)
    if re.search(r'@@[^\n]*@@', text):
        raise AssociationError('模板中仍有未替换标记')
    # 评论中的换行不能形成新 YAML 语句。
    header = ['# 自动生成的关联报告（设备位置来自 Excel，控制端位置来自 HA）：']
    header += ['# ' + ' '.join(line.split()) for line in report]
    return '\n'.join(header) + '\n\n' + text


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def validate_output(output, template, xlsx):
    path = Path(output)
    if path.suffix.lower() not in ('.yaml', '.yml'):
        raise AssociationError('自动化输出文件必须使用 .yaml 或 .yml 扩展名')
    if (path.name in ('crestron.yaml', 'automations_manual_template.yaml')
            or path.resolve() in (Path(template).resolve(), Path(xlsx).resolve())):
        raise AssociationError('输出路径不能覆盖模板、设备配置或输入 Excel')


def generate(xlsx, snapshot, output, template=DEFAULT_TEMPLATE, check=False):
    template = Path(template)
    validate_output(output, template, xlsx)
    rows = read_associations(xlsx)
    switch_map, occupancy_map, report = build_maps(rows, snapshot)
    content = render_template(template.read_text(encoding='utf-8'), switch_map, occupancy_map, report)
    for line in report:
        print(line)
    if not check:
        atomic_write(output, content)
        print(f'已生成自动化：{output}（关联报告也已写入文件顶部注释）')
    return content


def main(argv=None):
    parser = devices._ChineseArgumentParser(description=__doc__)
    parser.add_argument('xlsx', help='设备 .xlsx 文件')
    parser.add_argument('output', nargs='?', help='输出文件，默认与 Excel 同目录的 automations_manual.yaml')
    parser.add_argument('--registry', help='HA 注册表 JSON 快照，包含 entities/devices/areas/floors，可包含 states')
    parser.add_argument('--template', default=str(DEFAULT_TEMPLATE), help='自动化模板路径')
    parser.add_argument('--check', action='store_true', help='检查关联和模板，输出关联报告，不写文件')
    parser.add_argument('--check-xlsx', action='store_true', help='只检查 Excel 关联格式，不连接 HA、不生成文件')
    args = parser.parse_args(argv)
    try:
        if args.check_xlsx:
            rows = read_associations(args.xlsx)
            render_template(Path(args.template).read_text(encoding='utf-8'), {}, {}, [])
            print(f'Excel 关联检查通过：{len(rows)} 行设备配置；实体匹配将在读取 HA 注册表后检查。')
            return 0
        if not args.registry:
            parser.error('需要 --registry 注册表快照；设备必须先导入 HA')
        output = args.output or str(Path(args.xlsx).parent / 'automations_manual.yaml')
        if Path(output).resolve() == Path(args.registry).resolve():
            raise AssociationError('输出路径不能覆盖注册表输入')
        generate(args.xlsx, json.loads(Path(args.registry).read_text()), output, args.template, args.check)
    except (AssociationError, OSError, ValueError, KeyError) as error:
        print(f'[中止] {error}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
