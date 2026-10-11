#!/usr/bin/env node
// 部署 HA 原生仪表盘，需要 Node.js 22+，无需安装 npm 依赖。
import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { createInterface } from 'node:readline';

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = message => { throw new Error(message); };

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
    if (!object(doc) || Object.keys(doc).some(k => !['dashboard', 'config'].includes(k))) fail(`${file}：只允许 dashboard、config 两个顶层字段`);
    const d = doc.dashboard;
    if (!object(d) || !/^[a-z0-9]+(?:-[a-z0-9]+)+$/.test(d.url_path)) fail(`${file}：dashboard.url_path 必须为含连字符的小写地址`);
    if (Object.keys(d).some(k => !['url_path', 'title', 'icon', 'show_in_sidebar', 'require_admin'].includes(k))) fail(`${file}：未知 dashboard 字段`);
    if (typeof d.title !== 'string' || !d.title.trim()) fail(`${file}：缺少 dashboard.title`);
    if (d.icon !== undefined && (typeof d.icon !== 'string' || !d.icon.startsWith('mdi:'))) fail(`${file}：icon 应为 mdi: 图标`);
    for (const key of ['show_in_sidebar', 'require_admin']) {
      if (d[key] !== undefined && typeof d[key] !== 'boolean') fail(`${file}：${key} 必须为布尔值`);
    }
    if (!object(doc.config) || !Array.isArray(doc.config.views) || !doc.config.views.length || !doc.config.views.every(object)) fail(`${file}：config.views 必须是非空视图列表`);
    if (urls.has(d.url_path)) fail(`${file}：重复 url_path ${d.url_path}`);
    urls.add(d.url_path);
    return { file, dashboard: { show_in_sidebar: true, require_admin: false, ...d }, config: doc.config };
  });
}

export function overwritePrompt(readLine, log = console.log) {
  let all;
  return async name => {
    if (all !== undefined) return all;
    for (;;) {
      const answer = await readLine(`已存在 ${name}，是否覆盖？[y 覆盖 / n 跳过 / All yes 全部覆盖 / All no 全部跳过，默认 n] `);
      if (answer === null) {
        all = false;
        log('输入结束，跳过当前及后续已有页面；新页面仍会创建。');
        return false;
      }
      switch (answer.trim().toLowerCase().replace(/\s+/g, ' ')) {
        case 'y': return true;
        case '': case 'n': return false;
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

export async function deployDashboards(items, api, ask, backup, log = console.log) {
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
  let created = 0, overwritten = 0;
  for (const { item, old, previous } of selected) {
    const url = item.dashboard.url_path;
    // 用户确认期间可能编辑页面，写入前再次检查，避免覆盖刚发生的修改。
    const latest = (await api.call('lovelace/dashboards/list')).find(d => d.url_path === url);
    if (!isDeepStrictEqual(latest, old) || (old && !isDeepStrictEqual(await readConfig(api, url), previous))) fail(`${url} 在确认期间发生变化，停止部署，请重新检查`);
    if (!old && (await api.call('get_panels'))[url]) fail(`${url} 已被其他页面占用`);
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
  const options = {};
  let command = [];
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === '--') { command = argv.slice(i + 1); break; }
    if (flag === '--check') { options.check = true; continue; }
    const key = { '--dir': 'dir', '--remote-backup-dir': 'backup', '--host': 'host', '--ha-url': 'url' }[flag];
    if (!key || !argv[i + 1]) fail(`参数无效或缺少值：${flag}`);
    options[key] = argv[++i];
  }
  if (!options.dir) fail('必须指定 --dir');
  const items = loadDashboards(options.dir);
  if (options.check || !items.length) { console.log(`仪表盘配置检查通过：${items.length} 个页面`); return; }
  if (!options.backup) fail('必须指定 --remote-backup-dir');
  const backup = remoteBackup(command, options.backup);
  if (typeof WebSocket !== 'function') fail('部署仪表盘需要 Node.js 22 或更新版本');
  const auth = await authenticate(options, command);
  const api = new HaClient(auth.url, auth.token);
  auth.token = '';
  let input;
  try {
    await api.ready;
    // 首次提问时才读取标准输入，避免提前消耗用户输入。
    let iterator;
    const ask = overwritePrompt(async prompt => {
      if (!input) { input = createInterface({ input: process.stdin, crlfDelay: Infinity }); iterator = input[Symbol.asyncIterator](); }
      process.stdout.write(prompt);
      const result = await iterator.next();
      return result.done ? null : result.value;
    });
    const result = await deployDashboards(items, api, ask, backup);
    console.log(`仪表盘部署完成：新建 ${result.created}，覆盖 ${result.overwritten}，跳过 ${result.skipped}`);
  } finally { input?.close(); api.close(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main(process.argv.slice(2)).catch(err => { console.error(`错误：${err.message}`); process.exitCode = 1; });
}
