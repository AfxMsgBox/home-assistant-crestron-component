// HA 自定义卡片：使用当前登录会话，不保存令牌、现场设备清单或外部依赖。
export const POWER_TYPES = {
  light: { name: '灯光', states: ['on'], icon: 'mdi:lightbulb' },
  climate: { name: '空调', states: ['heat', 'cool', 'heat_cool', 'auto', 'dry', 'fan_only'], icon: 'mdi:air-conditioner' },
  switch: { name: '插座', states: ['on'], icon: 'mdi:power-socket' },
  fan: { name: '风扇', states: ['on'], icon: 'mdi:fan' },

};

// 实体位置优先，其次设备位置；按 ID 分组，重名楼层/房间也不会混在一起。
export function activeRows(states, registry) {
  if (!registry) return [];
  const index = (rows, key) => new Map(rows.map(row => [row[key], row]));
  const entities = index(registry.entities, 'entity_id'), devices = index(registry.devices, 'id');
  const areas = index(registry.areas, 'area_id'), floors = index(registry.floors, 'floor_id');
  const domains = Object.keys(POWER_TYPES);
  const rows = [];
  for (const state of Object.values(states || {})) {
    const domain = state.entity_id.split('.')[0], type = POWER_TYPES[domain];
    const entity = entities.get(state.entity_id) || {};
    if (!type || !state.state || entity.hidden_by || entity.disabled_by || entity.entity_category) continue;
    // switch 还包括扫地机等设备的功能开关；只收录明确标记为插座的实体。
    if (domain === 'switch' && state.attributes?.device_class !== 'outlet') continue;
    if (!type.states.includes(state.state)) continue;
    const device = devices.get(entity.device_id) || {};
    const area = areas.get(entity.area_id || device.area_id), floor = floors.get(area?.floor_id);
    rows.push({ id: state.entity_id, domain, state, area, floor,
      name: entity.name || state.attributes?.friendly_name || device.name_by_user || device.name || entity.original_name || state.entity_id });
  }
  const compare = (a, b) => String(a).localeCompare(String(b), 'zh-CN', { numeric: true });
  return rows.sort((a, b) => (a.floor ? 0 : 1) - (b.floor ? 0 : 1)
    || (a.floor?.level ?? 0) - (b.floor?.level ?? 0)
    || compare(a.floor?.name || '', b.floor?.name || '')
    || compare(a.floor?.floor_id || '', b.floor?.floor_id || '')
    || (a.area ? 0 : 1) - (b.area ? 0 : 1)
    || compare(a.area?.name || '', b.area?.name || '')
    || compare(a.area?.area_id || '', b.area?.area_id || '')
    || domains.indexOf(a.domain) - domains.indexOf(b.domain)
    || compare(a.name, b.name) || compare(a.id, b.id));
}

export function groupRows(rows) {
  const floors = new Map();
  for (const row of rows) {
    const floorId = row.floor?.floor_id ?? null, areaId = row.area?.area_id ?? null;
    if (!floors.has(floorId)) floors.set(floorId, { name: row.floor?.name || '未分配楼层', rooms: new Map() });
    const rooms = floors.get(floorId).rooms;
    if (!rooms.has(areaId)) rooms.set(areaId, { name: row.area?.name || '未分配房间', types: new Map() });
    const types = rooms.get(areaId).types;
    if (!types.has(row.domain)) types.set(row.domain, []);
    types.get(row.domain).push(row);
  }
  return floors;
}

// 点击时重新取当前范围；空列表不调用服务，避免被解释成关闭全部实体。
export async function turnOffActive(hass, registry, domain) {
  if (!['climate', 'light'].includes(domain)) throw new Error('不支持的关闭类型');
  if (hass.connection?.connected === false) throw new Error('HA 连接已断开');
  if (!registry) throw new Error('设备位置尚未加载，请稍后重试');
  const ids = activeRows(hass.states, registry).filter(row => row.domain === domain).map(row => row.id);
  if (!ids.length) return 0;
  await hass.callService(domain, 'turn_off', {}, { entity_id: ids });
  return ids.length;
}

const REGISTRIES = [
  ['entities', 'entity'], ['devices', 'device'], ['areas', 'area'], ['floors', 'floor'],
];

// 注册表事件触发刷新，30 秒校验用于断线重连/遗漏事件兜底。
// 先订阅再读取；刷新中的新事件置 dirty，避免用旧结果覆盖新位置。
export class RegistryWatch {
  constructor(connection, onChange, schedule = setInterval, cancel = clearInterval) {
    this.connection = connection;
    this.onChange = onChange;
    this.cancel = cancel;
    this.stopped = false;
    this.unsubscribers = [];
    this.timer = schedule(() => this.refresh(), 30000);
    this.ready = () => this.refresh();
    this.disconnected = () => this.onChange(null, 'HA 连接已断开，恢复后会自动更新。');
    connection.addEventListener?.('ready', this.ready);
    connection.addEventListener?.('disconnected', this.disconnected);
  }
  async start() {
    await Promise.all(REGISTRIES.map(async ([, name]) => {
      try {
        const unsubscribe = await this.connection.subscribeEvents(() => this.refresh(), `${name}_registry_updated`);
        if (this.stopped) await unsubscribe();
        else this.unsubscribers.push(unsubscribe);
      } catch {
        // 部分会话不允许订阅时仍可轮询；读取失败会在卡片上明确显示。
      }
    }));
    await this.refresh();
  }
  async refresh() {
    if (this.stopped) return;
    if (this.loading) { this.dirty = true; return; }
    this.loading = true;
    try {
      do {
        this.dirty = false;
        const lists = await Promise.all(REGISTRIES.map(([, name]) =>
          this.connection.sendMessagePromise({ type: `config/${name}_registry/list` })));
        if (!lists.every(Array.isArray)) throw new Error('HA 返回的设备位置数据无效');
        if (!this.stopped && !this.dirty && this.connection.connected !== false) {
          this.onChange(Object.fromEntries(REGISTRIES.map(([key], i) => [key, lists[i]])), '');
        }
      } while (this.dirty && !this.stopped);
    } catch {
      if (!this.stopped) this.onChange(null, '无法读取设备位置，请检查 HA 连接和当前用户权限；连接恢复后会自动重试。');
    } finally { this.loading = false; }
  }
  stop() {
    this.stopped = true;
    this.cancel(this.timer);
    this.connection.removeEventListener?.('ready', this.ready);
    this.connection.removeEventListener?.('disconnected', this.disconnected);
    for (const unsubscribe of this.unsubscribers) Promise.resolve().then(unsubscribe).catch(() => {});
    this.unsubscribers = [];
  }
}

const STYLE = `
:host { display:block; color:var(--primary-text-color); }
* { box-sizing:border-box; }
.toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:12px; }
button { font:inherit; color:inherit; cursor:pointer; }
.off { border:1px solid var(--divider-color); border-radius:12px; padding:10px 14px;
 background:var(--ha-card-background,var(--card-background-color)); display:flex; align-items:center; gap:6px; }
button:hover { background:var(--secondary-background-color); }
button:focus-visible { outline:2px solid var(--primary-color); outline-offset:2px; }
button:disabled { opacity:.45; cursor:default; }
.count,.state,.hint { color:var(--secondary-text-color); font-size:13px; }
.message { padding:12px 0; }
.error { color:var(--error-color,#db4437); }
.floors { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr)); gap:12px; align-items:start; }
ha-card { padding:14px; border-radius:var(--ha-card-border-radius,12px); }
h2 { font-size:19px; margin:0 0 12px; }
h3 { font-size:15px; margin:14px 0 4px; }
h4 { font-size:12px; font-weight:500; color:var(--secondary-text-color); margin:10px 0 2px; }
.row { display:flex; align-items:center; width:100%; gap:10px; padding:9px 0; border:0;
 border-bottom:1px solid var(--divider-color); text-align:start; background:transparent; min-height:44px; }
.row:last-child { border-bottom:0; }
ha-icon { flex:0 0 24px; color:var(--primary-color); }
.name { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.state { flex:0 0 auto; max-width:35%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
`;

// DOM 使用 textContent，设备名称不会作为 HTML 执行。无需 HACS 或构建步骤。
export class ActiveDevicesCard extends (globalThis.HTMLElement || class {}) {
  setConfig(config) { this.config = config; this.render(); }
  getCardSize() { return 8; }
  getGridOptions() { return { columns: 'full', rows: 'auto' }; }
  set hass(value) {
    this._hass = value;
    if (this.isConnected) this.watch();
    if (value.connection?.connected === false) {
      this.registry = null;
      this.registryError = 'HA 连接已断开，恢复后会自动更新。';
    }
    this.render();
  }
  connectedCallback() { this.watch(); this.render(); }
  disconnectedCallback() { this.watcher?.stop(); this.watcher = null; this.registry = null; }
  watch() {
    const connection = this._hass?.connection;
    if (!connection || this.watcher?.connection === connection) return;
    this.watcher?.stop();
    this.registry = null;
    this.watcher = new RegistryWatch(connection, (data, error) => {
      this.registry = data; this.registryError = error; this.render();
    });
    this.watcher.start();
  }
  async closeDomain(domain) {
    if (this.busy) return;
    this.busy = domain; this.actionMessage = ''; this.actionError = false; this.render();
    try {
      const count = await turnOffActive(this._hass, this.registry, domain);
      this.actionMessage = count ? `已发送 ${count} 个关闭请求，等待设备反馈。` : '当前没有需要关闭的设备。';
    } catch (error) {
      this.actionError = true;
      this.actionMessage = `关闭请求失败：${error.message || '请检查 HA 权限和设备连接'}`;
    } finally { this.busy = ''; this.render(); }
  }
  render() {
    if (!this._hass || !this.isConnected) return;
    const root = this.shadowRoot || this.attachShadow({ mode: 'open' });
    const rows = activeRows(this._hass.states, this.registry);
    // 非相关状态变化无需重建 DOM，减少焦点跳动。
    const signature = JSON.stringify([rows, this.registryError, !!this.registry, this.busy, this.actionMessage]);
    if (signature === this.signature) return;
    this.signature = signature;
    const focusKey = root.activeElement?.dataset.key;
    const element = (tag, text, className) => {
      const node = document.createElement(tag);
      if (text !== undefined) node.textContent = text;
      if (className) node.className = className;
      return node;
    };
    const style = element('style', STYLE);
    const toolbar = element('div', undefined, 'toolbar');
    for (const domain of ['climate', 'light']) {
      const count = rows.filter(row => row.domain === domain).length;
      const button = element('button', `${domain === 'climate' ? '关闭全屋空调' : '关闭全屋灯光'} (${count})`, 'off');
      button.dataset.key = domain;
      button.disabled = !count || !!this.busy || !this.registry;
      button.onclick = () => this.closeDomain(domain);
      toolbar.append(button);
    }
    toolbar.append(element('span', `${rows.length} 个已开启实体`, 'count'));
    const status = element('div', this.registryError || (!this.registry ? '正在读取设备位置…' : this.actionMessage || ''),
      `message ${this.registryError || this.actionError ? 'error' : 'hint'}`);
    status.setAttribute('role', 'status');
    const floors = element('div', undefined, 'floors');
    for (const floor of groupRows(rows).values()) {
      const card = element('ha-card');
      card.append(element('h2', floor.name));
      for (const room of floor.rooms.values()) {
        card.append(element('h3', room.name));
        for (const [domain, devices] of room.types) {
          card.append(element('h4', `${POWER_TYPES[domain].name} · ${devices.length}`));
          for (const row of devices) {
            const button = element('button', undefined, 'row');
            button.dataset.key = row.id;
            button.title = `${row.name} · ${row.id}`;
            const icon = element('ha-icon'); icon.setAttribute('icon', row.state.attributes?.icon || POWER_TYPES[domain].icon);
            const state = this._hass.formatEntityState ? this._hass.formatEntityState(row.state) : row.state.state;
            button.append(icon, element('span', row.name, 'name'), element('span', state, 'state'));
            button.onclick = () => this.dispatchEvent(new CustomEvent('hass-more-info', {
              detail: { entityId: row.id }, bubbles: true, composed: true,
            }));
            card.append(button);
          }
        }
      }
      floors.append(card);
    }
    if (this.registry && !rows.length) floors.append(element('p', '当前没有已开启的设备。', 'hint'));
    root.replaceChildren(style, toolbar, status, floors);
    if (focusKey) [...root.querySelectorAll('button')].find(node => node.dataset.key === focusKey)?.focus();
  }
}

if (globalThis.customElements && !customElements.get('crestron-active-devices')) {
  customElements.define('crestron-active-devices', ActiveDevicesCard);
}
