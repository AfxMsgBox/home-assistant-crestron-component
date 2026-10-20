import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { deployDashboards, overwritePrompt, loadDashboards, HaError, HaClient, normalizeUrl, remoteBackup, buildClimateDashboard, climateSnapshot, terminalUi, buildActiveDevicesDashboard, remoteResource, deployResources } from '../tools/deploy_dashboards.mjs';
import { activeRows, groupRows, turnOffActive, RegistryWatch, ActiveDevicesCard } from '../dashboard/resources/active-devices-card.js';

test('deployment UI aligns Chinese labels and respects TTY and NO_COLOR', () => {
  const original = { term: process.env.TERM, noColor: process.env.NO_COLOR };
  let output = '';
  const stream = { isTTY: true, write: text => { output += text; } };
  try {
    process.env.TERM = 'xterm-256color';
    delete process.env.NO_COLOR;
    terminalUi(stream).log('部署完成', 'success');
    assert.match(output, /\x1b\[32m/);
    process.env.NO_COLOR = '';
    output = '';
    terminalUi(stream).log('部署完成', 'success');
    assert.doesNotMatch(output, /\x1b/);
    delete process.env.NO_COLOR;
    stream.isTTY = false;
    output = '';
    terminalUi(stream).details([['页面数量', 2], ['SSH', 'root@example']]);
    const lines = output.trimEnd().split('\n');
    const width = s => [...s].reduce((n, c) => n + (c.charCodeAt(0) > 255 ? 2 : 1), 0);
    assert.equal(width(lines[0].split(' : ')[0]), width(lines[1].split(' : ')[0]));
    assert.doesNotMatch(output, /\x1b/);
  } finally {
    if (original.term === undefined) delete process.env.TERM; else process.env.TERM = original.term;
    if (original.noColor === undefined) delete process.env.NO_COLOR; else process.env.NO_COLOR = original.noColor;
  }
});

// 用本地临时目录模拟 SSH 服务器，执行真实的远端备份 shell，不连接现场。
test('file table aligns Chinese names, sizes and timestamps without terminal control characters', () => {
  let output = '';
  terminalUi({ isTTY: false, write: text => { output += text; } }).table(
    ['文件', '大小', '最后修改时间（UTC）'],
    [['中文.json', '6 B', '2024-01-01 00:00:00'],
      ['a\n.json', '2,048 B', '2024-01-01 00:00:00']], [1]);
  const lines = output.trimEnd().split('\n');
  assert.equal(lines.length, 4);
  assert.match(output, /a\\x0a.json/);
  const width = s => [...s].reduce((n, c) => n + (c.charCodeAt(0) > 255 ? 2 : 1), 0);
  assert.equal(width(lines[2].split('2024-01-01')[0]), width(lines[3].split('2024-01-01')[0]));
  assert.doesNotMatch(output, /\x1b/);
});

function remoteFixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'crestron-backup-test-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const ssh = join(root, 'ssh.cjs');
  writeFileSync(ssh, `const {execFileSync} = require('node:child_process');
const {readFileSync} = require('node:fs');
execFileSync('sh', ['-c', process.argv[3].replaceAll('/homeassistant', process.argv[2])],
  {input: readFileSync(0), stdio: ['pipe', 'pipe', 'pipe']});
`);
  const directory = '/homeassistant/crestron-backups/2026-10-10/153000-ABC123/dashboard';
  return { root, ssh, directory, backup: remoteBackup([process.execPath, ssh, root], directory),
    local: directory.replace('/homeassistant', root) };
}

test('dashboard backup is written on the server before save with exact JSON and private permissions', async t => {
  const remote = remoteFixture(t), f = fixture(['page-one']);
  f.configs.set('page-one', { views: [{ title: "中文 '$HOME' $(touch unwanted)\n第二行" }] });
  const previous = structuredClone(f.configs.get('page-one'));
  await deployDashboards([item('page-one')], f.api, async () => true, remote.backup, () => {});
  const file = join(remote.local, 'page-one.json');
  assert.deepEqual(JSON.parse(readFileSync(file, 'utf8')), { dashboard: f.dashboards[0], config: previous });
  assert.equal(statSync(file).mode & 0o777, 0o600);
  assert.deepEqual(readdirSync(remote.local), ['page-one.json']);
  assert.deepEqual(f.configs.get('page-one'), item('page-one').config);
});

test('server backup never overwrites an existing backup and cleans temporary files', async t => {
  const remote = remoteFixture(t);
  await remote.backup('page-one', { config: oldConfig });
  await assert.rejects(remote.backup('page-one', { config: 'changed' }), /无法保存服务器备份/);
  assert.deepEqual(JSON.parse(readFileSync(join(remote.local, 'page-one.json'), 'utf8')), { config: oldConfig });
  assert.deepEqual(readdirSync(remote.local), ['page-one.json']);
});

test('failed remote backup stops dashboard writes', async t => {
  const remote = remoteFixture(t), f = fixture(['page-one']);
  writeFileSync(remote.ssh, 'process.exit(1);');
  await assert.rejects(deployDashboards([item('page-one')], f.api, async () => true,
    remote.backup, () => {}), /无法保存服务器备份/);
  assert.equal(writes(f).length, 0);
});

test('server backup validates destination and name before executing SSH', async t => {
  const remote = remoteFixture(t);
  assert.throws(() => remoteBackup([], remote.directory), /SSH/);
  assert.throws(() => remoteBackup(['ssh'], '/tmp/backup'), /目录无效/);
  await assert.rejects(remote.backup('../escape', {}), /名称无效/);
});

const item = (url, config = { views: [{ title: 'new' }] }) => ({ dashboard: { url_path: url, title: '页面', show_in_sidebar: true, require_admin: false }, config });
const oldConfig = { views: [{ title: 'old' }] };
function fixture(urls = ['page-one', 'page-two']) {
  const dashboards = urls.map(url_path => ({ url_path, title: '旧标题', mode: 'storage', id: url_path.replaceAll('-', '_') }));
  const configs = new Map(urls.map(url => [url, structuredClone(oldConfig)]));
  const events = [], backups = [];
  const panels = {};
  const api = { async call(type, args = {}) {
    events.push({ type, args: structuredClone(args) });
    switch (type) {
      case 'lovelace/dashboards/list': return structuredClone(dashboards);
      case 'get_panels': return structuredClone(panels);
      case 'lovelace/config':
        if (!configs.has(args.url_path)) throw new HaError('config_not_found', 'No config');
        return structuredClone(configs.get(args.url_path));
      case 'lovelace/dashboards/create': dashboards.push({ ...args, mode: 'storage' }); return {};
      case 'lovelace/config/save': configs.set(args.url_path, structuredClone(args.config)); return null;
      default: throw new Error('Unexpected API ' + type);
    }
  } };
  const backup = async (url, content) => { events.push({ type: 'backup', url }); backups.push({ url, content }); return 'backup/' + url; };
  return { api, backup, backups, events, configs, dashboards, panels };
}
function answers(values) {
  const prompts = [];
  const ask = overwritePrompt(async prompt => { prompts.push(prompt); return values.shift() ?? null; }, () => {});
  return { ask, prompts };
}
const writes = f => f.events.filter(e => /create|save/.test(e.type));

test('y and n affect only the matching URLs; backup precedes overwrite', async () => {
  const f = fixture(), a = answers(['y', 'n']);
  const result = await deployDashboards([item('page-one'), item('page-two'), item('page-new')], f.api, a.ask, f.backup, () => {});
  assert.deepEqual(result, { created: 1, overwritten: 1, skipped: 1 });
  assert.deepEqual(f.configs.get('page-two'), oldConfig);
  assert.deepEqual(f.backups[0].content.config, oldConfig);
  assert.equal(f.backups[1].content.dashboard, null);
  assert.equal(a.prompts.length, 2);
  assert(f.events.findIndex(e => e.type === 'backup') < f.events.findIndex(e => e.type === 'lovelace/config/save'));
  assert.equal(f.dashboards[0].title, '旧标题');
});

for (const [input, overwritten, skipped] of [['All yes', 2, 0], [' All   NO ', 0, 2]]) {
  test(input + ' persists for existing pages, while new pages are created', async () => {
    const f = fixture(), a = answers([input]);
    assert.deepEqual(await deployDashboards([item('page-one'), item('page-new'), item('page-two')], f.api, a.ask, f.backup, () => {}), { created: 1, overwritten, skipped });
    assert.equal(a.prompts.length, 1);
  });
}

test('invalid answers repeat; Enter is yes; EOF means all no', async () => {
  const a = answers(['maybe', '', null]);
  assert.equal(await a.ask('one'), true);
  assert.equal(await a.ask('two'), false);
  assert.equal(await a.ask('three'), false);
  assert.equal(a.prompts.length, 3);
  assert.equal(await answers(['n', 'y']).ask('one'), false);
  assert.equal(await answers(['yes']).ask('one'), true);
  assert.equal(await answers(['no']).ask('one'), false);
});

test('a new invocation resets All yes state', async () => {
  assert.equal(await answers(['All yes']).ask('one'), true);
  assert.equal(await answers(['n']).ask('one'), false);
});

test('no-input mode does not overwrite existing pages', async () => {
  const f = fixture();
  await deployDashboards([item('page-one'), item('page-two')], f.api, answers([]).ask, f.backup, () => {});
  assert.equal(writes(f).length, 0);
  assert.equal(f.backups.length, 0);
});

test('same titles at different URLs are distinct dashboards', async () => {
  const f = fixture();
  f.dashboards[0].title = '页面';
  let asked = false;
  await deployDashboards([item('page-other')], f.api, async () => { asked = true; }, f.backup, () => {});
  assert.equal(asked, false);
  assert.deepEqual(f.configs.get('page-one'), oldConfig);
});

test('built-in and YAML collisions are rejected before any writes', async () => {
  for (const kind of ['panel', 'yaml']) {
    const f = fixture();
    if (kind === 'panel') f.panels['page-blocked'] = {};
    else f.dashboards.push({ url_path: 'page-blocked', mode: 'yaml' });
    await assert.rejects(deployDashboards([item('page-new'), item('page-blocked')], f.api, async () => true, f.backup), /不能覆盖/);
    assert.equal(writes(f).length, 0);
  }
});

test('all confirmation choices are collected before writes', async () => {
  const f = fixture();
  await deployDashboards([item('page-new'), item('page-one'), item('page-two')], f.api, async () => { assert.equal(writes(f).length, 0); return true; }, f.backup, () => {});
});

test('an existing dashboard without saved config can be backed up and saved', async () => {
  const f = fixture();
  f.configs.delete('page-one');
  await deployDashboards([item('page-one')], f.api, async () => true, f.backup, () => {});
  assert.equal(f.backups[0].content.config, null);
  assert.equal(writes(f).length, 1);
});

test('read permission failures must not be treated as a missing dashboard', async () => {
  const f = fixture(), call = f.api.call;
  f.api.call = async (type, args) => { if (type === 'lovelace/config') throw new HaError('unauthorized', 'Denied'); return call(type, args); };
  await assert.rejects(deployDashboards([item('page-one')], f.api, async () => true, f.backup), /Denied/);
  assert.equal(writes(f).length, 0);
});

test('concurrent edits during a prompt stop the overwrite', async () => {
  const f = fixture();
  await assert.rejects(deployDashboards([item('page-one')], f.api, async () => { f.configs.set('page-one', { views: [{ title: 'user edit' }] }); return true; }, f.backup), /发生变化/);
  assert.equal(writes(f).length, 0);
});

test('backup failure prevents writes', async () => {
  const f = fixture();
  await assert.rejects(deployDashboards([item('page-one')], f.api, async () => true, async () => { throw new Error('disk full'); }), /disk full/);
  assert.equal(writes(f).length, 0);
});

test('failed save preserves backup and stops later pages', async () => {
  const f = fixture(), call = f.api.call;
  f.api.call = async (type, args) => { if (type === 'lovelace/config/save') throw new Error('write failed'); return call(type, args); };
  await assert.rejects(deployDashboards([item('page-one'), item('page-two')], f.api, async () => true, f.backup, () => {}), /备份：backup\/page-one/);
  assert.equal(f.backups.length, 1);
});

test('mismatched readback fails deployment', async () => {
  const f = fixture(), call = f.api.call;
  f.api.call = async (type, args) => type === 'lovelace/config/save' ? null : call(type, args);
  await assert.rejects(deployDashboards([item('page-one')], f.api, async () => true, f.backup, () => {}), /回读配置不一致/);
});

test('recursive JSON discovery rejects duplicate URLs and malformed files before deployment', () => {
  const dir = mkdtempSync(join(tmpdir(), 'crestron-dashboard-test-'));
  try {
    mkdirSync(join(dir, 'nested'));
    writeFileSync(join(dir, 'one.json'), JSON.stringify(item('page-one')));
    writeFileSync(join(dir, 'nested', 'two.json'), JSON.stringify(item('page-two')));
    writeFileSync(join(dir, 'README.md'), 'ignored');
    assert.equal(loadDashboards(dir).length, 2);
    writeFileSync(join(dir, 'nested', 'two.json'), JSON.stringify(item('page-one')));
    assert.throws(() => loadDashboards(dir), /重复/);
    writeFileSync(join(dir, 'nested', 'two.json'), '{');
    assert.throws(() => loadDashboards(dir), /有效 JSON/);
    for (const invalid of [item('../climate'), item('climate'), item('page-one', { views: [] }), { ...item('page-one'), unknown: true }, { ...item('page-one'), dashboard: { ...item('page-one').dashboard, mode: 'yaml' } }]) {
      writeFileSync(join(dir, 'nested', 'two.json'), JSON.stringify(invalid));
      assert.throws(() => loadDashboards(dir));
    }
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

const climateTemplate = () => loadDashboards(fileURLToPath(new URL('../dashboard', import.meta.url))).find(item => item.generator?.type === 'climate');
const activeTemplate = () => loadDashboards(fileURLToPath(new URL('../dashboard', import.meta.url))).find(item => item.generator?.type === 'active_devices');

function powerData() {
  const data = climateData();
  data.entities = [{ entity_id: 'light.lamp', platform: 'crestron', device_id: 'd' },
    { entity_id: 'climate.unit', platform: 'another_brand', device_id: 'd', area_id: 'b' },
    { entity_id: 'switch.plug', platform: 'third_brand', area_id: 'a' },
    { entity_id: 'switch.setting', entity_category: 'config' },
    { entity_id: 'light.hidden', hidden_by: 'user' },
    { entity_id: 'fan.disabled', disabled_by: 'integration' }];
  data.states = [
    ['light.lamp', 'off'], ['climate.unit', 'heat'], ['switch.plug', 'on'],
    ['fan.unregistered', 'on'], ['media_player.tv', 'paused'], ['humidifier.unit', 'on'],
    ['water_heater.tank', 'eco'], ['vacuum.robot', 'cleaning'],
    ['cover.curtain', 'open'], ['binary_sensor.presence', 'on'], ['sensor.power', '50'],
    ['automation.rule', 'on'], ['input_boolean.virtual', 'on'],
    ['switch.setting', 'on'], ['light.hidden', 'on'], ['fan.disabled', 'on'],
  ].map(([entity_id, state]) => ({ entity_id, state, attributes: { friendly_name: entity_id, ...(entity_id === 'switch.plug' ? { device_class: 'outlet' } : {}) } }));
  return data;
}

const stateMap = data => Object.fromEntries(data.states.map(state => [state.entity_id, state]));

test('live card spans integrations, inherits position, excludes inactive and non-power entities', () => {
  const data = powerData(), rows = activeRows(stateMap(data), data);
  assert.deepEqual(rows.map(row => row.id), ['switch.plug', 'climate.unit', 'fan.unregistered']);
  assert.equal(rows.find(row => row.id === 'climate.unit').area.area_id, 'b');
  assert.deepEqual([...groupRows(rows).values()].map(row => row.name), ['示例一层', '示例二层', '未分配楼层']);
});

test('new entities, off feedback, renames and moved rooms update without deployment', () => {
  const data = powerData(), states = stateMap(data);
  states['light.new'] = { entity_id: 'light.new', state: 'on', attributes: { friendly_name: '新灯' } };
  assert(activeRows(states, data).some(row => row.id === 'light.new'));
  data.entities.push({ entity_id: 'light.new', area_id: 'a', name: '改名后的灯' });
  assert.equal(activeRows(states, data).find(row => row.id === 'light.new').floor.floor_id, 'lower');
  data.areas[0].floor_id = 'upper';
  assert.equal(activeRows(states, data).find(row => row.id === 'light.new').floor.floor_id, 'upper');
  assert.equal(activeRows(states, data).find(row => row.id === 'light.new').name, '改名后的灯');
  for (const state of ['off', 'unknown', 'unavailable']) {
    states['light.new'].state = state;
    assert(!activeRows(states, data).some(row => row.id === 'light.new'));
  }
  delete states['light.new'];
  assert(!activeRows(states, data).some(row => row.id === 'light.new'));
  assert.equal(groupRows(activeRows({}, data)).size, 0);
});

test('bulk close reads current targets and never sends empty, brightness or cooling commands', async () => {
  const data = powerData(), calls = [], hass = { states: stateMap(data),
    callService: async (...args) => calls.push(args) };
  assert.equal(await turnOffActive(hass, data, 'light'), 0);
  assert.equal(calls.length, 0);
  hass.states['light.lamp'].state = 'on';
  assert.equal(await turnOffActive(hass, data, 'light'), 1);
  assert.equal(await turnOffActive(hass, data, 'climate'), 1);
  assert.deepEqual(calls, [['light', 'turn_off', {}, { entity_id: ['light.lamp'] }],
    ['climate', 'turn_off', {}, { entity_id: ['climate.unit'] }]]);
  hass.callService = async () => { throw new Error('禁止操作'); };
  await assert.rejects(turnOffActive(hass, data, 'light'), /禁止操作/);
  await assert.rejects(turnOffActive(hass, null, 'light'), /尚未加载/);
  await assert.rejects(turnOffActive(hass, data, 'switch'), /不支持/);
  hass.connection = { connected: false };
  await assert.rejects(turnOffActive(hass, data, 'light'), /断开/);
});

test('same named locations stay separate, names sort naturally, and empty groups disappear', () => {
  const data = powerData();
  data.floors[0].name = data.floors[1].name;
  data.areas[1].name = data.areas[0].name;
  data.entities.push({ entity_id: 'light.second', area_id: 'a' });
  data.states.push({ entity_id: 'light.second', state: 'on', attributes: { friendly_name: '灯2' } });
  data.states[0].attributes.friendly_name = '灯10'; data.states[0].state = 'on';
  const rows = activeRows(stateMap(data), data);
  assert.equal(groupRows(rows).size, 3);
  assert.deepEqual(rows.filter(row => row.domain === 'light').map(row => row.name), ['灯2', '灯10']);
});

test('active deployment stores only generic card config and skipped page installs no assets', async () => {
  const f = fixture([]), doc = activeTemplate(), installed = [];
  await deployDashboards([doc], f.api, async () => true, f.backup, () => {}, 120,
    async items => installed.push(...items));
  assert.deepEqual(f.configs.get(doc.dashboard.url_path), buildActiveDevicesDashboard(doc));
  assert.equal(installed[0].resources[0].name, 'active-devices-card.js');
  assert.equal(f.events.some(e => /call_service|registry|get_states/.test(e.type)), false);
  const skipped = fixture(['dashboard-active-devices']);
  await deployDashboards([doc], skipped.api, async () => false, skipped.backup, () => {}, 120,
    async items => assert.equal(items.length, 0));
  assert.equal(writes(skipped).length, 0);
});

test('active template rejects embedded device data and extra generator keys', t => {
  const dir = mkdtempSync(join(tmpdir(), 'crestron-active-test-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const { file, ...doc } = activeTemplate();
  doc.generator.entities = ['light.private'];
  writeFileSync(join(dir, 'page.json'), JSON.stringify(doc));
  assert.throws(() => loadDashboards(dir), /仅接受 type/);
});

function climateData() {
  return {
    floors: [{ floor_id: 'upper', name: '示例二层', level: 2 }, { floor_id: 'lower', name: '示例一层', level: 1 }],
    areas: [{ area_id: 'a', name: '示例房间甲', floor_id: 'lower' }, { area_id: 'b', name: '示例房间乙', floor_id: 'upper' }],
    devices: [{ id: 'd', area_id: 'a', name: '示例空调' }],
    entities: [{ entity_id: 'climate.example_a', device_id: 'd' }, { entity_id: 'climate.example_b', device_id: 'd', area_id: 'b', name: '自定义名称' },
      { entity_id: 'climate.disabled', disabled_by: 'user' }, { entity_id: 'climate.hidden', hidden_by: 'user' }],
    states: ['example_a', 'example_b', 'unregistered', 'disabled', 'hidden'].map(name => ({ entity_id: `climate.${name}`, state: 'unavailable',
      attributes: { friendly_name: name, supported_features: 9, fan_modes: ['auto', 'quiet', 'high'] } })),
  };
}
function withClimateApi(f, data = climateData(), config = { state: 'RUNNING' }) {
  const call = f.api.call;
  const replies = { get_config: config, get_states: data.states,
    'config/entity_registry/list': data.entities, 'config/device_registry/list': data.devices,
    'config/area_registry/list': data.areas, 'config/floor_registry/list': data.floors };
  f.api.call = async (type, args) => type in replies ? structuredClone(replies[type]) : call(type, args);
}

test('template has no household entities and generates compact cards with real HA IDs and capabilities', () => {
  const doc = climateTemplate(), original = structuredClone(doc), data = climateData();
  assert.deepEqual(doc.config.views[0].sections, []);
  assert.equal(doc.generator.card.entity, undefined);
  const config = buildClimateDashboard(doc, data), sections = config.views[0].sections;
  assert.deepEqual(sections.map(s => s.cards[0].heading), ['示例一层', '示例二层', '未分配楼层']);
  const cards = sections.flatMap(s => s.cards.slice(1));
  assert.deepEqual(cards.map(c => c.entity), ['climate.example_a', 'climate.example_b', 'climate.unregistered']);
  assert.equal(cards[1].name, '示例房间乙 · 自定义名称');
  for (const card of cards) {
    assert.deepEqual(card.grid_options, { columns: 6, rows: 3 });
    assert.equal(card.icon_tap_action.perform_action, 'climate.toggle');
    assert.deepEqual(card.icon_tap_action.target, { entity_id: card.entity });
    assert.equal(card.tap_action.action, 'more-info');
    assert.equal(card.features_position, 'bottom');
    assert.deepEqual(card.features.map(f => f.type), ['target-temperature', 'climate-fan-modes']);
    assert.deepEqual(card.features[1].fan_modes, ['auto', 'quiet', 'high']);
  }
  assert.deepEqual(doc, original);
  data.states[0].attributes = { friendly_name: '示例房间甲空调', supported_features: 0 };
  const card = buildClimateDashboard(doc, data).views[0].sections[0].cards[1];
  assert.equal(card.name, '示例房间甲空调');
  assert.deepEqual(card.features, []);
  assert.throws(() => buildClimateDashboard(doc, { ...data, states: [] }), /未找到/);
});

test('dynamic schema rejects unknown generator types, embedded IDs and nonempty views', t => {
  const dir = mkdtempSync(join(tmpdir(), 'crestron-template-test-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  for (const change of [d => { d.generator.type = 'unknown'; }, d => { d.generator.card.entity = 'climate.example'; },
    d => { d.generator.card.icon_tap_action = {}; }, d => { d.config.views[0].sections.push({}); },
    d => { d.generator.card.features = 'invalid'; }]) {
    const { file, ...doc } = climateTemplate();
    change(doc);
    writeFileSync(join(dir, 'page.json'), JSON.stringify(doc));
    assert.throws(() => loadDashboards(dir));
  }
});

test('dynamic generation saves the complete runtime config and backup precedes writes', async () => {
  const f = fixture([]), doc = climateTemplate(), data = climateData();
  withClimateApi(f, data);
  const result = await deployDashboards([doc], f.api, async () => true, f.backup, () => {});
  assert.equal(result.created, 1);
  assert.deepEqual(f.configs.get(doc.dashboard.url_path), buildClimateDashboard(doc, data));
  assert(f.events.findIndex(e => e.type === 'backup') < f.events.findIndex(e => e.type === 'lovelace/config/save'));
  assert.equal(f.events.some(e => e.type === 'call_service'), false);
});

test('empty data and registry errors stop all page writes; skipped templates require no device API', async () => {
  for (const apiError of [false, true]) {
    const f = fixture([]);
    withClimateApi(f, { entities: [], devices: [], areas: [], floors: [], states: [] });
    if (apiError) {
      const call = f.api.call;
      f.api.call = (type, args) => type === 'config/floor_registry/list' ? Promise.reject(new Error('权限不足')) : call(type, args);
    }
    await assert.rejects(deployDashboards([item('page-new'), climateTemplate()], f.api, async () => true, f.backup, () => {}), /未找到|权限不足/);
    assert.equal(writes(f).length, 0);
    assert.equal(f.backups.length, 0);
  }
  const f = fixture(['dashboard-climate']);
  assert.equal((await deployDashboards([climateTemplate()], f.api, async () => false, f.backup, () => {})).skipped, 1);
});

test('snapshot waits for RUNNING and missing active entities, includes unavailable and ignores disabled', async () => {
  const f = fixture([]), data = climateData();
  withClimateApi(f, data);
  const call = f.api.call;
  let round = 0, elapsed = 0;
  f.api.call = async (type, args) => {
    if (type === 'get_config' && round === 0) return { state: 'STARTING' };
    if (type === 'get_states' && round === 1) return [];
    return call(type, args);
  };
  const pause = async ms => { elapsed += ms; round++; };
  const result = await climateSnapshot(f.api, () => {}, 10, pause, () => elapsed);
  assert.equal(round, 2);
  assert.deepEqual(result.states, data.states);
  round = 0; elapsed = 0;
  await assert.rejects(climateSnapshot(f.api, () => {}, 1, pause, () => elapsed), /超时/);
  withClimateApi(f, data, { state: 'RUNNING', safe_mode: true });
  await assert.rejects(climateSnapshot(f.api), /安全/);
});

test('URL handling preserves HTTPS and a reverse-proxy prefix, rejecting credentials', () => {
  assert.equal(normalizeUrl('https://ha.example/ha'), 'https://ha.example/ha/');
  for (const url of ['file:///etc/auth', 'https://user:secret@ha.example', 'http://ha/#climate', 'http://ha/?key=secret']) assert.throws(() => normalizeUrl(url));
});

class Socket extends EventTarget {
  readyState = 1;
  constructor(url) { super(); this.url = url; Socket.instance = this; queueMicrotask(() => this.message({ type: 'auth_required' })); }
  message(data) { this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(data) })); }
  send(text) {
    const msg = JSON.parse(text);
    if (msg.type === 'auth') queueMicrotask(() => this.message({ type: msg.access_token === 'valid' ? 'auth_ok' : 'auth_invalid' }));
    else this.last = msg;
  }
  close() { this.readyState = 3; this.dispatchEvent(new Event('close')); }
}

test('WebSocket auth, ID correlation, HA errors and disconnect handling', async () => {
  const api = new HaClient('https://ha.example/ha/', 'valid', Socket, 1000);
  await api.ready;
  assert.equal(Socket.instance.url, 'wss://ha.example/ha/api/websocket');
  const p = api.call('get_panels');
  await Promise.resolve();
  const msg = Socket.instance.last;
  Socket.instance.message({ type: 'result', id: msg.id + 1, success: true, result: 'wrong' });
  Socket.instance.message({ type: 'result', id: msg.id, success: true, result: 'right' });
  assert.equal(await p, 'right');
  const error = api.call('lovelace/config');
  await Promise.resolve();
  Socket.instance.message({ type: 'result', id: Socket.instance.last.id, success: false, error: { code: 'unauthorized', message: 'Denied' } });
  await assert.rejects(error, err => err instanceof HaError && err.code === 'unauthorized');
  const disconnected = api.call('get_panels');
  await Promise.resolve();
  api.close();
  await assert.rejects(disconnected, /连接已断开/);
});

test('WebSocket bad auth and timed-out command fail cleanly', async () => {
  const bad = new HaClient('http://ha/', 'invalid', Socket, 1000);
  await assert.rejects(bad.ready, /认证失败/);
  const api = new HaClient('http://ha/', 'valid', Socket, 20);
  await assert.rejects(api.call('get_panels'), /超时/);
  assert.equal(api.pending.size, 0);
  api.close();
});

function registryFixture() {
  const data = powerData(), events = new Map(), listeners = new Map();
  let reads = 0, stopped = 0, interval, cancelled = false;
  const connection = {
    connected: true,
    subscribeEvents: async (callback, name) => { events.set(name, callback); return () => { stopped++; }; },
    addEventListener: (name, cb) => listeners.set(name, cb),
    removeEventListener: name => listeners.delete(name),
    sendMessagePromise: async ({ type }) => {
      reads++;
      return { entity: data.entities, device: data.devices, area: data.areas, floor: data.floors }[type.split('/')[1].split('_')[0]];
    },
  };
  const changes = [];
  const watcher = new RegistryWatch(connection, (registry, error) => changes.push({ registry, error }),
    cb => { interval = cb; return 42; }, id => { assert.equal(id, 42); cancelled = true; });
  return { data, events, listeners, connection, changes, watcher, interval: () => interval(),
    get reads() { return reads; }, get stopped() { return stopped; }, get cancelled() { return cancelled; } };
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test('registry events, periodic checks and reconnect update metadata and release subscriptions', async () => {
  const f = registryFixture();
  await f.watcher.start();
  assert.equal(f.reads, 4); assert.equal(f.events.size, 4);
  f.data.areas[0].name = '新房间';
  f.events.get('area_registry_updated')(); await tick();
  assert.equal(f.changes.at(-1).registry.areas[0].name, '新房间');
  f.connection.connected = false;
  f.listeners.get('disconnected')();
  assert.equal(f.changes.at(-1).registry, null);
  f.connection.connected = true;
  f.listeners.get('ready')(); await tick();
  assert(f.changes.at(-1).registry);
  await f.interval();
  assert.equal(f.reads, 16);
  f.watcher.stop(); await tick();
  assert.equal(f.stopped, 4); assert.equal(f.cancelled, true); assert.equal(f.listeners.size, 0);
  await f.watcher.refresh(); assert.equal(f.reads, 16);
});

test('failed registry reads disable actions until recovery; late responses do not update detached card', async () => {
  const f = registryFixture(), send = f.connection.sendMessagePromise;
  f.connection.sendMessagePromise = async () => { throw new Error('offline'); };
  await f.watcher.start();
  assert.equal(f.changes.at(-1).registry, null); assert.match(f.changes.at(-1).error, /无法读取/);
  f.connection.sendMessagePromise = send;
  await f.interval(); assert(f.changes.at(-1).registry);
  let resolveRead;
  f.connection.sendMessagePromise = () => new Promise(resolve => { resolveRead = resolve; });
  // 四个在途响应共享一个 Promise，断开后不能写回数据。
  const pending = new Promise(resolve => { resolveRead = resolve; });
  f.connection.sendMessagePromise = () => pending;
  const read = f.watcher.refresh(), count = f.changes.length;
  f.watcher.stop(); resolveRead([]); await read;
  assert.equal(f.changes.length, count);
});

test('registry events during a refresh trigger a second read and suppress stale metadata', async () => {
  const f = registryFixture(), send = f.connection.sendMessagePromise;
  let finish;
  const pending = new Promise(resolve => { finish = resolve; });
  let calls = 0;
  f.connection.sendMessagePromise = msg => ++calls <= 4 ? pending : send(msg);
  const starting = f.watcher.start(); await tick();
  f.events.get('entity_registry_updated')();
  finish([]); await starting;
  assert.equal(calls, 8); assert.equal(f.changes.length, 1);
  assert(f.changes[0].registry.entities.length > 0);
  f.watcher.stop();
});

function resourceFixture() {
  const f = fixture([]), call = f.api.call, resources = [];
  f.api.call = async (type, args = {}) => {
    if (type === 'get_config') return { state: 'RUNNING' };
    if (type === 'lovelace/info') return { resource_mode: 'storage' };
    if (type === 'lovelace/resources') return structuredClone(resources);
    if (type === 'lovelace/resources/create') {
      f.events.push({ type });
      resources.push({ id: 'resource-one', url: args.url, type: args.res_type }); return;
    }
    if (type === 'lovelace/resources/update') {
      f.events.push({ type });
      const old = resources.find(row => row.id === args.resource_id);
      assert(old); Object.assign(old, { url: args.url, type: args.res_type }); return;
    }
    return call(type, args);
  };
  return { ...f, resources };
}

test('resource deploy backs up registry and old file, uploads once, registers module with content hash', async t => {
  const remote = remoteFixture(t), f = resourceFixture(), doc = activeTemplate();
  const resource = doc.resources[0], local = join(remote.root, 'www/crestron', resource.name);
  mkdirSync(join(remote.root, 'www/crestron'), { recursive: true });
  writeFileSync(local, 'old module');
  f.resources.push({ id: 'old-id', url: '/local/crestron/active-devices-card.js?v=old', type: 'module' });
  await deployResources([doc, doc], f.api, remote.backup,
    remoteResource([process.execPath, remote.ssh, remote.root], remote.directory), () => {});
  assert.equal(readFileSync(join(remote.local, 'resources', resource.name), 'utf8'), 'old module');
  assert.deepEqual(readFileSync(local), resource.content);
  assert.equal(statSync(local).mode & 0o777, 0o644);
  assert.equal(f.resources.length, 1);
  assert.equal(f.resources[0].url, resource.url);
  assert.equal(f.resources[0].id, 'old-id');
  assert.match(f.resources[0].url, /\?v=[a-f0-9]{64}$/);
  const backup = JSON.parse(readFileSync(join(remote.local, 'card-resources.json')));
  assert.equal(backup.resources[0].url, '/local/crestron/active-devices-card.js?v=old');
  assert.equal(f.events.filter(e => e.type === 'lovelace/resources/update').length, 1);
  assert.equal(readdirSync(join(remote.root, 'www/crestron')).length, 1);
});

test('resource upload failure or YAML resource mode stops before any page save', async () => {
  const doc = activeTemplate(), f = resourceFixture();
  await assert.rejects(deployDashboards([doc], f.api, async () => true, f.backup, () => {}, 120,
    items => deployResources(items, f.api, f.backup, async () => { throw new Error('disk full'); })), /disk full/);
  assert.equal(writes(f).length, 0);
  const call = f.api.call;
  f.api.call = (type, args) => type === 'lovelace/info' ? { resource_mode: 'yaml' } : call(type, args);
  await assert.rejects(deployResources([doc], f.api, f.backup, async () => assert.fail('must not upload')), /存储模式/);
});

test('unchanged module reuses registration, while resource backup failure prevents upload', async () => {
  const f = resourceFixture(), doc = activeTemplate(), resource = doc.resources[0];
  f.resources.push({ id: 'r', type: 'module', url: resource.url });
  let uploads = 0;
  await deployResources([doc], f.api, f.backup, async () => { uploads++; }, () => {});
  assert.equal(uploads, 1); assert.equal(writes(f).length, 0);
  await assert.rejects(deployResources([doc], f.api, async () => { throw new Error('backup failed'); },
    async () => assert.fail('must not upload')), /backup failed/);
});

test('resource source rejects traversal, absent assets and malformed lists before SSH', t => {
  const dir = mkdtempSync(join(tmpdir(), 'card-source-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  for (const resources of [['../secret.js'], ['/root/private.js'], ['resources/missing.js'], 'bad']) {
    writeFileSync(join(dir, 'page.json'), JSON.stringify({ ...item('page-one'), resources }));
    assert.throws(() => loadDashboards(dir));
  }
});

test('active state rules only include lights, climates, outlets and fans', () => {
  for (const [domain, on, off] of [
    ['climate', ['heat', 'cool', 'auto', 'heat_cool', 'dry', 'fan_only'], ['off', 'unknown', 'unavailable']],
    ['light', ['on'], ['off', 'unknown', 'unavailable']],
    ['fan', ['on'], ['off', 'unknown', 'unavailable']],
    ['switch', [], ['on', 'off']],
    ['media_player', [], ['on', 'playing', 'paused']],
    ['water_heater', [], ['eco', 'on']],
    ['humidifier', [], ['on']],
    ['vacuum', [], ['on', 'cleaning', 'returning']],
  ]) {
    const id = `${domain}.test`, data = { entities: [], devices: [], floors: [], areas: [] };
    for (const state of on) assert.equal(activeRows({ [id]: { entity_id: id, state } }, data).length, 1);
    for (const state of off) assert.equal(activeRows({ [id]: { entity_id: id, state } }, data).length, 0);
  }
});

test('concurrent page edit prevents even resource upload', async () => {
  const doc = activeTemplate(), f = resourceFixture();
  f.dashboards.push({ ...doc.dashboard, mode: 'storage' });
  f.configs.set(doc.dashboard.url_path, oldConfig);
  await assert.rejects(deployDashboards([doc], f.api, async () => {
    f.configs.set(doc.dashboard.url_path, { views: [{ title: '用户刚修改的配置' }] }); return true;
  }, f.backup, () => {}, 120, async () => assert.fail('must not upload resources')), /发生变化/);
});

// 轻量 DOM 替身检查用户交互，不代替真实浏览器的主题和移动端布局验证。
test('card renders safe names, opens more-info and reports service failure without hiding a device', async () => {
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    setAttribute(key, value) { this[key] = value; }
    querySelectorAll(tag) { return this.children.flatMap(child => [
      ...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag),
    ]); }
    focus() {}
  }
  const previous = globalThis.document;
  globalThis.document = { createElement: tag => new Element(tag) };
  try {
    const data = powerData(), state = stateMap(data);
    state['light.lamp'].state = 'on';
    state['light.lamp'].attributes.friendly_name = '<img src=x onerror=alert(1)>';
    const card = new ActiveDevicesCard(), root = new Element('root');
    card.isConnected = true; card.shadowRoot = root; card.registry = data;
    card._hass = { states: state, callService: async () => { throw new Error('拒绝请求'); } };
    card.render();
    const row = root.querySelectorAll('button').find(node => node.dataset.key === 'light.lamp');
    assert.equal(row.children[1].textContent, '<img src=x onerror=alert(1)>');
    assert.equal(root.querySelectorAll('img').length, 0);
    let event;
    card.dispatchEvent = value => { event = value; };
    row.onclick(); assert.equal(event.type, 'hass-more-info'); assert.equal(event.detail.entityId, 'light.lamp');
    await card.closeDomain('light');
    assert.match(card.actionMessage, /拒绝请求/);
    assert(root.querySelectorAll('button').some(node => node.dataset.key === 'light.lamp'));
    assert.equal(card.busy, '');
  } finally { globalThis.document = previous; }
});

// 未填写 entity_category 的功能开关也必须排除，不能靠名称猜测插座。
test('outlets require explicit device class, while robot feature switches stay excluded', () => {
  const data = { entities: [], devices: [], areas: [], floors: [] };
  const states = Object.fromEntries([
    ['switch.outlet', 'outlet', 'on'], ['switch.off_outlet', 'outlet', 'off'],
    ['switch.robot_feature', undefined, 'on'], ['switch.named_plug', 'switch', 'on'],
    ['switch.unknown_outlet', 'outlet', 'unavailable'],
  ].map(([entity_id, device_class, state]) => [entity_id, { entity_id, state,
    attributes: { device_class, friendly_name: '插座' } }]));
  assert.deepEqual(activeRows(states, data).map(row => row.id), ['switch.outlet']);
  states['switch.new_outlet'] = { entity_id: 'switch.new_outlet', state: 'on', attributes: { device_class: 'outlet' } };
  assert.equal(activeRows(states, data).length, 2);
  data.entities.push({ entity_id: 'switch.new_outlet', entity_category: 'config' });
  assert.equal(activeRows(states, data).length, 1);
});
