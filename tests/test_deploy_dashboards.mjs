import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { deployDashboards, overwritePrompt, loadDashboards, HaError, HaClient, normalizeUrl, remoteBackup, buildClimateDashboard, climateSnapshot } from '../tools/deploy_dashboards.mjs';

// 用本地临时目录模拟 SSH 服务器，执行真实的远端备份 shell，不连接现场。
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

test('invalid answers repeat; Enter is n; EOF means all no', async () => {
  const a = answers(['yes', '', null]);
  assert.equal(await a.ask('one'), false);
  assert.equal(await a.ask('two'), false);
  assert.equal(await a.ask('three'), false);
  assert.equal(a.prompts.length, 3);
  assert.equal(await answers(['n', 'y']).ask('one'), false);
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

const climateTemplate = () => loadDashboards(fileURLToPath(new URL('../dashboard', import.meta.url)))[0];
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
