#!/usr/bin/env node
// 部署 HA 原生仪表盘，需要 Node.js 22+，无需安装 npm 依赖。
import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync, statSync, realpathSync } from 'node:fs';
import { join, resolve, relative, basename } from 'node:path';
import { pathToFileURL } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { createInterface } from 'node:readline';
import { createHash } from 'node:crypto';

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = message => { throw new Error(message); };

// 与 Shell/Python 使用相同的状态标签；重定向日志不写 ANSI 控制码。
export function terminalUi(stream = process.stdout) {
  const paint = (text, code) => stream.isTTY && !('NO_COLOR' in process.env) && process.env.TERM !== 'dumb'
    ? `\x1b[${code}m${text}\x1b[0m` : text;
  const width = text => [...String(text)].reduce((n, c) => {
    const cp = c.codePointAt(0);
    return n + (/\p{Mark}/u.test(c) ? 0 : cp >= 0x2e80 && cp <= 0xa4cf || cp >= 0xac00 && cp <= 0xd7a3
      || cp >= 0xff01 && cp <= 0xff60 || cp >= 0x1f300 && cp <= 0x1faff ? 2 : 1);
  }, 0);
  return {
    log(text, level = 'info') {
      const [label, code] = { info: ['信息', '36'], step: ['进行', '1;34'], success: ['完成', '32'],
        warning: ['警告', '33'], error: ['错误', '31'] }[level];
      stream.write(`${paint(`[${label}]`, code)} ${text}\n`);
    },
    heading(text) { stream.write(`\n${paint(`── ${text} ──`, '1;34')}\n`); },
    details(rows) {
      const size = Math.max(0, ...rows.map(([key]) => width(key)));
      for (const [key, value] of rows) stream.write(`       ${paint(key + ' '.repeat(size - width(key)), '36')} : ${value}\n`);
    },
    table(headers, values, right = []) {
      const clean = value => String(value).replace(/[\x00-\x1f\x7f-\x9f]/gu,
        c => `\\x${c.charCodeAt(0).toString(16).padStart(2, '0')}`);
      const rows = [headers, ...values].map(row => row.map(clean));
      const widths = headers.map((_, i) => Math.max(...rows.map(row => width(row[i]))));
      rows.forEach((row, index) => {
        const line = row.map((value, i) => {
          const pad = ' '.repeat(widths[i] - width(value));
          return right.includes(i) ? pad + value : value + pad;
        }).join('  ').trimEnd();
        stream.write('  ' + (index ? line : paint(line, '1;36')) + '\n');
        if (!index) stream.write('  ' + paint(widths.map(size => '─'.repeat(size)).join('  '), '36') + '\n');
      });
    },
    prompt(text) { stream.write(paint('[选择]', '1;33') + ' ' + text); },
  };
}

export function loadDashboards(directory) {
  const files = [];
  function walk(dir) {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) walk(path);
      else if (entry.isFile() && entry.name.endsWith('.json')) files.push(path);
    }
  }
  walk(directory);
  const urls = new Set();
  return files.sort().map(file => {
    let doc;
    try { doc = JSON.parse(readFileSync(file, 'utf8')); }
    catch { fail(`${file}：无法读取有效 JSON`); }
    if (!object(doc) || Object.keys(doc).some(k => !['dashboard', 'config', 'generator', 'resources'].includes(k))) fail(`${file}：只允许 dashboard、config、generator、resources 顶层字段`);
    const d = doc.dashboard;
    if (!object(d) || !/^[a-z0-9]+(?:-[a-z0-9]+)+$/.test(d.url_path)) fail(`${file}：dashboard.url_path 必须为含连字符的小写地址`);
    if (d.url_path === 'card-resources') fail(`${file}：card-resources 为资源备份保留名称`);
    if (Object.keys(d).some(k => !['url_path', 'title', 'icon', 'show_in_sidebar', 'require_admin'].includes(k))) fail(`${file}：未知 dashboard 字段`);
    if (typeof d.title !== 'string' || !d.title.trim()) fail(`${file}：缺少 dashboard.title`);
    if (d.icon !== undefined && (typeof d.icon !== 'string' || !d.icon.startsWith('mdi:'))) fail(`${file}：icon 应为 mdi: 图标`);
    for (const key of ['show_in_sidebar', 'require_admin']) {
      if (d[key] !== undefined && typeof d[key] !== 'boolean') fail(`${file}：${key} 必须为布尔值`);
    }
    if (!object(doc.config) || !Array.isArray(doc.config.views) || !doc.config.views.length || !doc.config.views.every(object)) fail(`${file}：config.views 必须是非空视图列表`);
    if (doc.generator !== undefined) {
      const g = doc.generator;
      if (!object(g) || !['climate', 'active_devices'].includes(g.type)) fail(`${file}：generator 类型须为 climate 或 active_devices`);
      if (doc.config.views.length !== 1 || doc.config.views[0].type !== 'sections' || !Array.isArray(doc.config.views[0].sections) || doc.config.views[0].sections.length) fail(`${file}：动态页面须包含一个 sections 视图，sections 留空`);
      if (g.type === 'climate') {
        if (Object.keys(g).some(k => !['type', 'card'].includes(k)) || !object(g.card) || g.card.type !== 'tile') fail(`${file}：climate 需要 tile 卡片模板`);
        if (g.card.entity !== undefined || g.card.name !== undefined || g.card.icon_tap_action !== undefined) fail(`${file}：卡片实体、名称和电源动作由目标 HA 自动生成，请勿写入模板`);
        if (g.card.features !== undefined && (!Array.isArray(g.card.features) || !g.card.features.every(object))) fail(`${file}：卡片 features 必须是对象列表`);
      } else if (Object.keys(g).some(k => k !== 'type')) {
        fail(`${file}：active_devices 仅接受 type，设备和分组由目标 HA 自动生成`);
      }
    }
    if (doc.resources !== undefined && !Array.isArray(doc.resources)) fail(`${file}：resources 必须为文件路径列表`);
    const resources = (doc.resources || []).map(path => {
      if (typeof path !== 'string' || !/^resources\/[a-z0-9-]+\.js$/.test(path)) fail(`${file}：资源必须是 resources/ 下的小写 JS 文件`);
      const source = resolve(directory, path);
      if (!realpathSync(source).startsWith(realpathSync(directory) + '/')) fail(`${file}：资源路径越界`);
      try { execFileSync(process.execPath, ['--check', source], { stdio: 'pipe', timeout: 15000 }); }
      catch { fail(`${file}：卡片 JavaScript 语法检查失败：${path}`); }
      const content = readFileSync(source);
      const hash = createHash('sha256').update(content).digest('hex');
      return { file: source, name: basename(source), hash, content,
        url: `/local/crestron/${basename(source)}?v=${hash}` };
    });
    if (doc.generator?.type === 'active_devices' && !resources.some(r => r.name === 'active-devices-card.js')) fail(`${file}：已开启设备页面缺少卡片资源`);
    if (urls.has(d.url_path)) fail(`${file}：重复 url_path ${d.url_path}`);
    urls.add(d.url_path);
    return { file, dashboard: { show_in_sidebar: true, require_admin: false, ...d }, config: doc.config, resources,
      ...(doc.generator === undefined ? {} : { generator: doc.generator }) };
  });
}

const isClimate = row => typeof row.entity_id === 'string' && row.entity_id.startsWith('climate.');
const visible = row => !row.disabled_by && !row.hidden_by;

// 等待启动及实体加载完成；接口失败直接报错，不能把缺失数据当成空页面覆盖。
async function entitySnapshot(api, matches, label, log = console.log, waitSeconds = 120,
  pause = ms => new Promise(resolvePause => setTimeout(resolvePause, ms)), now = Date.now) {
  const deadline = now() + waitSeconds * 1000;
  for (;;) {
    const config = await api.call('get_config');
    if (config.safe_mode || config.recovery_mode) fail('HA 处于安全/恢复模式，停止生成空调页面');
    let missing = [];
    if (config.state === 'RUNNING') {
      const [entities, devices, areas, floors, states] = await Promise.all([
        'config/entity_registry/list', 'config/device_registry/list',
        'config/area_registry/list', 'config/floor_registry/list', 'get_states',
      ].map(type => api.call(type)));
      if (![entities, devices, areas, floors, states].every(Array.isArray)) fail('HA 返回的注册表或状态列表无效');
      const loaded = new Set(states.filter(matches).map(row => row.entity_id));
      missing = entities.filter(row => matches(row) && visible(row) && !loaded.has(row.entity_id));
      if (!missing.length) return { entities, devices, areas, floors, states };
    }
    if (now() >= deadline) fail(`等待${label}实体超时${missing.length ? `，仍有 ${missing.length} 个未加载` : '，HA 尚未完成启动'}；页面未修改`);
    log(`等待 HA 完成启动及${label}实体加载${missing.length ? `（剩余 ${missing.length} 个）` : ''}...`);
    await pause(2000);
  }
}

export const climateSnapshot = (api, ...args) => entitySnapshot(api, isClimate, '空调', ...args);
// 卡片直接读取浏览器当前 HA 连接，不把现场实体清单固化在页面内。
export function buildActiveDevicesDashboard(item) {
  const config = structuredClone(item.config);
  config.views[0].sections = [{ type: 'grid', column_span: 3, cards: [
    { type: 'custom:crestron-active-devices', grid_options: { columns: 'full', rows: 'auto' } },
  ] }];
  return config;
}

// 只生成配置，不调用设备服务；现场名称和实体 ID 仅保留在内存与 HA 中。
export function buildClimateDashboard(item, snapshot) {
  const index = (rows, key) => new Map(rows.map(row => [row[key], row]));
  const entities = index(snapshot.entities, 'entity_id'), devices = index(snapshot.devices, 'id');
  const areas = index(snapshot.areas, 'area_id'), floors = index(snapshot.floors, 'floor_id');
  const rows = snapshot.states.filter(isClimate).filter(state => visible(entities.get(state.entity_id) || {})).map(state => {
    const entity = entities.get(state.entity_id) || {}, device = devices.get(entity.device_id) || {};
    const area = areas.get(entity.area_id || device.area_id), floor = floors.get(area?.floor_id);
    const name = entity.name || state.attributes?.friendly_name || device.name_by_user || device.name || entity.original_name || state.entity_id;
    const room = area?.name || '未分配房间';
    return { state, floor, room, name: name.includes(room) ? name : `${room} · ${name}` };
  });
  if (!rows.length) fail('未找到可显示的空调实体，页面未修改；仅部署设备可使用 --skip-dashboard');
  const compare = (a, b) => a.localeCompare(b, 'zh-CN', { numeric: true });
  rows.sort((a, b) => (a.floor ? 0 : 1) - (b.floor ? 0 : 1)
    || (a.floor?.level ?? 0) - (b.floor?.level ?? 0)
    || compare(a.floor?.name || '', b.floor?.name || '') || compare(a.room, b.room)
    || compare(a.name, b.name) || compare(a.state.entity_id, b.state.entity_id));
  const sections = new Map();
  for (const { state, floor, name } of rows) {
    const key = floor?.floor_id ?? null;
    if (!sections.has(key)) sections.set(key, { type: 'grid', cards: [{ type: 'heading', heading: floor?.name || '未分配楼层', heading_style: 'title' }] });
    const card = structuredClone(item.generator.card), attrs = state.attributes || {};
    card.entity = state.entity_id;
    card.name = name;
    card.icon_tap_action = { action: 'perform-action', perform_action: 'climate.toggle', target: { entity_id: state.entity_id } };
    // 温度与风速按实际能力显示，兼容其他品牌和自定义风速名称。
    card.features = (card.features || []).filter(feature => {
      if (feature.type === 'target-temperature') return Boolean(attrs.supported_features & 1);
      if (feature.type === 'climate-fan-modes') {
        if (!(attrs.supported_features & 8) || !Array.isArray(attrs.fan_modes) || !attrs.fan_modes.length) return false;
        feature.fan_modes = [...attrs.fan_modes];
      }
      return true;
    });
    sections.get(key).cards.push(card);
  }
  const config = structuredClone(item.config);
  config.views[0].sections = [...sections.values()];
  return config;
}

export function overwritePrompt(readLine, log = console.log) {
  let all;
  return async name => {
    if (all !== undefined) return all;
    for (;;) {
      const answer = await readLine(`已存在 ${name}，是否覆盖？[Y/n / All yes / All no，默认 Y] `);
      if (answer === null) {
        all = false;
        log('输入结束，跳过当前及后续已有页面；新页面仍会创建。');
        return false;
      }
      switch (answer.trim().toLowerCase().replace(/\s+/g, ' ')) {
        case '': case 'y': case 'yes': case '是': return true;
        case 'n': case 'no': case '否': return false;
        case 'all yes': all = true; return true;
        case 'all no': all = false; return false;
        default: log('请输入 y（覆盖）、n（跳过）、All yes（全部覆盖）或 All no（全部跳过）。');
      }
    }
  };
}

export class HaError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

// 按请求 ID 匹配 WebSocket 响应，断开时结束尚未完成的调用。
export class HaClient {
  constructor(url, token, Socket = WebSocket, timeout = 15000) {
    const wsUrl = new URL('api/websocket', url);
    wsUrl.protocol = wsUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    this.socket = new Socket(wsUrl.href);
    this.pending = new Map();
    this.seq = 0;
    this.timeout = timeout;
    this.ready = new Promise((resolveReady, rejectReady) => {
      const timer = setTimeout(() => { rejectReady(new Error('HA 认证超时')); this.close(); }, timeout);
      this.socket.addEventListener('message', ({ data }) => {
        let msg;
        try { msg = JSON.parse(data); } catch { this.close(); return; }
        if (msg.type === 'auth_required') this.socket.send(JSON.stringify({ type: 'auth', access_token: token }));
        if (msg.type === 'auth_ok') { token = ''; clearTimeout(timer); resolveReady(); }
        if (msg.type === 'auth_invalid') { token = ''; clearTimeout(timer); rejectReady(new Error('HA 认证失败，请检查管理员会话或 HA_TOKEN')); this.close(); }
        if (msg.type === 'result') {
          const p = this.pending.get(msg.id);
          if (p) {
            this.pending.delete(msg.id);
            clearTimeout(p.timer);
            if (msg.success) p.resolve(msg.result);
            else p.reject(new HaError(msg.error?.code, msg.error?.message || 'HA API 错误'));
          }
        }
      });
      const disconnected = () => {
        token = '';
        clearTimeout(timer);
        rejectReady(new Error('HA WebSocket 连接失败或已断开'));
        for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(new Error('HA 连接已断开，写入结果可能需要回读确认')); }
        this.pending.clear();
      };
      this.socket.addEventListener('error', disconnected);
      this.socket.addEventListener('close', disconnected);
    });
  }
  async call(type, args = {}) {
    await this.ready;
    if (this.socket.readyState !== 1) fail('HA 连接不可用');
    const id = ++this.seq;
    return new Promise((resolveCall, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`${type} 超时，停止部署，请回读确认状态`)); }, this.timeout);
      this.pending.set(id, { resolve: resolveCall, reject, timer });
      try { this.socket.send(JSON.stringify({ ...args, id, type })); }
      catch { clearTimeout(timer); this.pending.delete(id); reject(new Error('HA 发送失败')); }
    });
  }
  close() { this.socket.close(); }
}

async function readConfig(api, url) {
  try { return await api.call('lovelace/config', { url_path: url }); }
  catch (err) { if (err instanceof HaError && err.code === 'config_not_found') return null; throw err; }
}

export async function deployDashboards(items, api, ask, backup, log = console.log, waitSeconds = 120, installResources = async items => {
  if (items.some(item => item.resources?.length)) fail('缺少卡片资源部署器');
}) {
  const dashboards = await api.call('lovelace/dashboards/list');
  const panels = await api.call('get_panels');
  const planned = [];
  // 写入前先检查所有页面地址冲突，并读取待备份的旧配置。
  for (const item of items) {
    const url = item.dashboard.url_path;
    const old = dashboards.find(d => d.url_path === url);
    if ((old && old.mode !== 'storage') || (!old && panels[url])) fail(`${url} 属于内置或 YAML 页面，不能覆盖`);
    const previous = old ? await readConfig(api, url) : null;
    planned.push({ item, old, previous });
  }
  const selected = [];
  for (const plan of planned) {
    const { item, old } = plan;
    if (old && !await ask(`${item.dashboard.title} (${item.dashboard.url_path})`)) { log(`跳过：${item.dashboard.url_path}`); continue; }
    selected.push(plan);
  }
  // 全部动态页面生成成功后才开始备份/写入；跳过的页面不读取设备数据。
  const climates = selected.filter(plan => plan.item.generator?.type === 'climate');
  if (climates.length) {
    const snapshot = await climateSnapshot(api, log, waitSeconds);
    for (const plan of climates) {
      plan.item = { ...plan.item, config: buildClimateDashboard(plan.item, snapshot) };
      log(`已生成空调页面：${plan.item.config.views[0].sections.length} 个楼层分区`);
    }
  }
  for (const plan of selected.filter(plan => plan.item.generator?.type === 'active_devices')) {
    plan.item = { ...plan.item, config: buildActiveDevicesDashboard(plan.item) };
    log('已准备实时设备页面：新增设备、状态和位置由浏览器自动更新');
  }
  async function assertUnchanged({ item, old, previous }) {
    const url = item.dashboard.url_path;
    // 用户确认期间可能编辑页面，写入前再次检查，避免覆盖刚发生的修改。
    const latest = (await api.call('lovelace/dashboards/list')).find(d => d.url_path === url);
    if (!isDeepStrictEqual(latest, old) || (old && !isDeepStrictEqual(await readConfig(api, url), previous))) fail(`${url} 在确认期间发生变化，停止部署，请重新检查`);
    if (!old && (await api.call('get_panels'))[url]) fail(`${url} 已被其他页面占用`);
  }
  // 资源也属于覆盖内容，用户等待期间的页面修改必须在资源写入前检查。
  for (const plan of selected) await assertUnchanged(plan);
  await installResources(selected.map(plan => plan.item));
  let created = 0, overwritten = 0;
  for (const plan of selected) {
    const { item, old, previous } = plan;
    const url = item.dashboard.url_path;
    await assertUnchanged(plan);
    const backupPath = await backup(url, { dashboard: old || null, config: previous });
    log(`备份：${backupPath}`);
    if (!old) await api.call('lovelace/dashboards/create', item.dashboard);
    try {
      await api.call('lovelace/config/save', { url_path: url, config: item.config });
      if (!isDeepStrictEqual(await readConfig(api, url), item.config)) fail('回读配置不一致');
    } catch (err) {
      fail(`${url} 部署未完成：${err.message}。未自动回滚，请核对远端状态；备份：${backupPath}`);
    }
    if (old) overwritten++; else created++;
    log(`${old ? '已覆盖' : '已创建'}并校验：${url}`);
  }
  return { created, overwritten, skipped: items.length - selected.length };
}

function sshJson(command, script) {
  if (!command.length) fail('缺少 SSH 连接参数');
  try {
    return JSON.parse(execFileSync(command[0], [...command.slice(1), 'sh -s'], {
      input: script, encoding: 'utf8', timeout: 30000, stdio: ['pipe', 'pipe', 'pipe'],
    }));
  } catch { fail('无法通过 SSH 读取 HA 信息/管理员会话；请检查连接、jq、权限，或设置 HA_URL 和 HA_TOKEN'); }
}

export function normalizeUrl(input) {
  const url = new URL(input);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) fail('HA 地址必须是无凭据、查询参数和片段的 HTTP(S) 地址');
  if (!url.pathname.endsWith('/')) url.pathname += '/';
  return url.href;
}

// 备份内容通过标准输入传输，不拼入命令；远端写入成功后才允许修改页面。
export function remoteBackup(command, directory) {
  if (!command.length) fail('服务器备份需要 SSH 连接参数');
  if (!/^\/homeassistant\/crestron-backups\/\d{4}-\d{2}-\d{2}\/\d{6}-[A-Za-z0-9]+\/dashboard$/.test(directory)) fail('服务器仪表盘备份目录无效');
  return async (url, content) => {
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)+$/.test(url)) fail('仪表盘备份名称无效');
    const file = `${directory}/${url}.json`;
    const script = `set -eu
umask 077
mkdir -p '${directory}'
partial=$(mktemp '${directory}/.backup.XXXXXX')
trap 'rm -f -- "$partial"' EXIT
trap 'exit 1' HUP INT TERM
cat > "$partial"
ln "$partial" '${file}'
`;
    try {
      execFileSync(command[0], [...command.slice(1), script], {
        input: JSON.stringify(content, null, 2) + '\n', encoding: 'utf8',
        timeout: 30000, stdio: ['pipe', 'pipe', 'pipe'],
      });
    } catch { fail(`无法保存服务器备份：${file}，已停止该页面写入；请检查 SSH、磁盘空间和权限`); }
    return file;
  };
}

// 只允许写项目专用资源目录；旧文件和资源注册信息随本次页面一并备份。
export function remoteResource(command, directory) {
  remoteBackup(command, directory); // 复用连接及备份路径校验。
  return async resource => {
    if (!/^[a-z0-9-]+\.js$/.test(resource.name)
      || !/^[a-f0-9]{64}$/.test(resource.hash)
      || createHash('sha256').update(resource.content).digest('hex') !== resource.hash) fail('卡片资源无效');
    const target = `/homeassistant/www/crestron/${resource.name}`;
    const script = `set -eu
umask 077
for path in /homeassistant/www /homeassistant/www/crestron '${target}' '${directory}/resources'; do
    test ! -L "$path" || { echo '资源路径不能是符号链接' >&2; exit 1; }
done
mkdir -p '${directory}/resources'
mkdir -p /homeassistant/www/crestron
chmod 755 /homeassistant/www/crestron
test ! -e '${directory}/resources/${resource.name}'
test ! -e '${directory}/resources/${resource.name}.absent'
if test -e '${target}'; then
    test -f '${target}'
    cp -p '${target}' '${directory}/resources/${resource.name}'
else
    touch '${directory}/resources/${resource.name}.absent'
fi
partial=$(mktemp /homeassistant/www/crestron/.upload.XXXXXX)
trap 'rm -f -- "$partial"' EXIT
trap 'exit 1' HUP INT TERM
cat > "$partial"
actual=$(sha256sum "$partial")
test "\${actual%% *}" = '${resource.hash}'
chmod 644 "$partial"
mv "$partial" '${target}'
`;
    try {
      execFileSync(command[0], [...command.slice(1), script], {
        input: resource.content, timeout: 30000, stdio: ['pipe', 'pipe', 'pipe'],
      });
    } catch { fail(`卡片上传失败：${resource.name}；请检查服务器权限、空间和备份 ${directory}`); }
  };
}

export async function deployResources(items, api, backup, upload, log = console.log) {
  const resources = new Map();
  for (const item of items) for (const resource of item.resources || []) {
    const previous = resources.get(resource.name);
    if (previous && previous.hash !== resource.hash) fail(`资源名称冲突：${resource.name}`);
    resources.set(resource.name, resource);
  }
  if (!resources.size) return;
  const config = await api.call('get_config');
  if (config.safe_mode || config.recovery_mode) fail('HA 处于安全/恢复模式，不能部署卡片资源');
  if ((await api.call('lovelace/info')).resource_mode !== 'storage') fail('卡片自动部署需要 HA 使用存储模式管理仪表盘资源，请先在 HA 中调整资源配置');
  const previous = await api.call('lovelace/resources');
  const plans = [...resources.values()].map(resource => {
    const path = resource.url.split('?')[0];
    const matches = previous.filter(row => row.url.split('?')[0] === path);
    if (matches.length > 1) fail(`HA 中重复注册了资源 ${path}，请先整理`);
    return { resource, old: matches[0] };
  });
  await backup('card-resources', { resources: previous });
  for (const { resource, old } of plans) {
    const latest = (await api.call('lovelace/resources')).filter(row => row.url.split('?')[0] === resource.url.split('?')[0]);
    if (!isDeepStrictEqual(latest, old ? [old] : [])) fail('资源注册在部署期间发生变化，已停止');
    await upload(resource);
    if (!old || old.url !== resource.url || old.type !== 'module') {
      await api.call(`lovelace/resources/${old ? 'update' : 'create'}`, {
        ...(old ? { resource_id: old.id } : {}), url: resource.url, res_type: 'module',
      });
    }
    const readback = (await api.call('lovelace/resources')).filter(row => row.url.split('?')[0] === resource.url.split('?')[0]);
    if (readback.length !== 1 || readback[0].url !== resource.url || readback[0].type !== 'module') fail('资源注册回读不一致，请核对 HA 和本次备份');
    log(`卡片已上传并注册：${resource.name}`);
  }
}

async function authenticate(options, command) {
  let url = options.url || process.env.HA_URL;
  if (!url) {
    const info = sshJson(command, 'set -eu\nha core info --raw-json\n');
    const core = info.data || info;
    if (!Number.isInteger(core.port) || core.port < 1 || core.port > 65535) fail('无法识别 HA HTTP 端口，请指定 --ha-url');
    if (!/^[A-Za-z0-9._-]+$/.test(options.host || '')) fail('缺少有效主机名');
    url = `${core.ssl ? 'https' : 'http'}://${options.host}:${core.port}/`;
  }
  url = normalizeUrl(url);
  if (process.env.HA_TOKEN) return { url, token: process.env.HA_TOKEN };
  // 经 root SSH 复用有效所有者浏览器会话；凭据仅保留在内存中，不写文件和日志。
  const session = sshJson(command, `set -eu
jq -ce '.data as $d | ($d.users | map(select(.is_owner and .is_active)) | first | .id) as $owner | ($d.refresh_tokens | map(select(.user_id == $owner and .token_type == "normal")) | sort_by(.created_at) | last) as $r | if $r == null then error("没有可用的所有者会话") else {token:$r.token,client_id:$r.client_id} end' /homeassistant/.storage/auth
`);
  const response = await fetch(new URL('auth/token', url), {
    method: 'POST', redirect: 'error', signal: AbortSignal.timeout(15000),
    body: new URLSearchParams({ grant_type: 'refresh_token', refresh_token: session.token, client_id: session.client_id }),
  });
  if (!response.ok) fail('无法换取 HA 临时令牌，请设置管理员 HA_TOKEN');
  const body = await response.json();
  if (typeof body.access_token !== 'string') fail('HA 未返回有效令牌');
  return { url, token: body.access_token };
}

export async function main(argv) {
  const ui = terminalUi();
  const options = {};
  let command = [];
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === '--') { command = argv.slice(i + 1); break; }
    if (flag === '--check') { options.check = true; continue; }
    const key = { '--dir': 'dir', '--remote-backup-dir': 'backup', '--host': 'host', '--ha-url': 'url', '--wait-seconds': 'wait' }[flag];
    if (!key || !argv[i + 1]) fail(`参数无效或缺少值：${flag}`);
    options[key] = argv[++i];
  }
  if (!options.dir) fail('必须指定 --dir');
  const waitSeconds = Number(options.wait ?? 120);
  if (!Number.isInteger(waitSeconds) || waitSeconds < 1 || waitSeconds > 9999) fail('--wait-seconds 必须在 1..9999 之间');
  const items = loadDashboards(options.dir);
  if (options.check || !items.length) { ui.log(`仪表盘配置检查通过：${items.length} 个页面`, 'success'); return; }
  if (!options.backup) fail('必须指定 --remote-backup-dir');
  const backup = remoteBackup(command, options.backup);
  if (typeof WebSocket !== 'function') fail('部署仪表盘需要 Node.js 22 或更新版本');
  const auth = await authenticate(options, command);
  const api = new HaClient(auth.url, auth.token);
  auth.token = '';
  let input;
  try {
    await api.ready;
    ui.heading('部署仪表盘');
    ui.details([['页面数量', items.length], ['备份目录', options.backup]]);
    ui.log('页面模板文件（动态页面按目标 HA 实体生成）');
    ui.table(['文件', '大小', '最后修改时间（UTC）'], items.map(item => {
      const stat = statSync(item.file);
      return [relative(options.dir, item.file), `${stat.size.toLocaleString('en-US')} B`,
        stat.mtime.toISOString().slice(0, 19).replace('T', ' ')];
    }), [1]);
    ui.log('覆盖选择：回车或 y 覆盖，n 跳过；All yes 全部覆盖，All no 全部跳过。');
    // 首次提问时才读取标准输入，避免提前消耗用户输入。
    let iterator;
    const ask = overwritePrompt(async prompt => {
      if (!input) { input = createInterface({ input: process.stdin, crlfDelay: Infinity }); iterator = input[Symbol.asyncIterator](); }
      ui.prompt(prompt);
      const result = await iterator.next();
      if (!process.stdin.isTTY) process.stdout.write('\n');
      return result.done ? null : result.value;
    }, text => ui.log(text, 'warning'));
    const result = await deployDashboards(items, api, ask, backup,
      text => ui.log(text, /^(已覆盖|已创建)/u.test(text) ? 'success' : 'info'), waitSeconds,
      selected => {
        const resources = [...new Map(selected.flatMap(item => (item.resources || []).map(r => [r.file, r]))).values()];
        if (resources.length) {
          ui.log('准备上传卡片资源');
          ui.table(['文件', '大小', '最后修改时间（UTC）'], resources.map(resource => {
            const stat = statSync(resource.file);
            return [resource.name, `${stat.size.toLocaleString('en-US')} B`,
              stat.mtime.toISOString().slice(0, 19).replace('T', ' ')];
          }), [1]);
        }
        return deployResources(selected, api, backup, async resource => {
          await remoteResource(command, options.backup)(resource);
          const response = await fetch(new URL(resource.url, auth.url), {
            signal: AbortSignal.timeout(15000), cache: 'no-store', redirect: 'error',
          });
          const content = Buffer.from(await response.arrayBuffer());
          if (!response.ok || createHash('sha256').update(content).digest('hex') !== resource.hash) {
            fail('卡片已上传但 /local 访问校验失败，页面未保存；请检查 HA 静态资源路由，首次创建 www 后可能需要重启 Core');
          }
        }, text => ui.log(text));
      });
    ui.log('仪表盘部署完成', 'success');
    ui.details([['新建', result.created], ['覆盖', result.overwritten], ['跳过', result.skipped]]);
  } finally { input?.close(); api.close(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main(process.argv.slice(2)).catch(err => { terminalUi(process.stderr).log(`错误：${err.message}`, 'error'); process.exitCode = 1; });
}
