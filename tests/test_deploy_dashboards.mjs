import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { deployDashboards, overwritePrompt, loadDashboards, HaError, HaClient, normalizeUrl } from '../tools/deploy_dashboards.mjs';

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

test('checked-in sample dashboard keeps its card compact with all controls', () => {
  const [doc] = loadDashboards(fileURLToPath(new URL('../dashboard', import.meta.url)));
  const sections = doc.config.views[0].sections;
  assert.deepEqual(sections.map(s => s.cards.filter(c => c.type === 'tile').length), [1]);
  const ids = new Set();
  for (const card of sections.flatMap(s => s.cards).filter(c => c.type === 'tile')) {
    ids.add(card.entity);
    assert.deepEqual(card.grid_options, { columns: 6, rows: 3 });
    assert.equal(card.icon_tap_action.perform_action, 'climate.toggle');
    assert.deepEqual(card.icon_tap_action.target, { entity_id: card.entity });
    assert.equal(card.tap_action.action, 'more-info');
    assert.equal(card.features_position, 'bottom');
    assert.deepEqual(card.features.map(f => f.type), ['target-temperature', 'climate-fan-modes']);
  }
  assert.equal(ids.size, 1);
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
