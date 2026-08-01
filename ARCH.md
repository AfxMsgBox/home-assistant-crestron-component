# 架构说明（ARCHITECTURE）

面向程序员与 AI 的实现说明：只讲**结构、约束和不变量**，不讲用法（用法见 `README.md`、`tools/README.md`）。

版本：`custom_components/crestron` v0.4.0，domain = `crestron`，`iot_class = local_push`。

---

## 1. 系统边界

Home Assistant 自定义集成，把 Crestron 控制系统（CP4N 等）的 **XSIG（Intersystem Communication）** join 与 HA 实体双向打通。

```
Crestron 控制系统                        Home Assistant
┌───────────────┐                       ┌──────────────────────────────────────┐
│ TCP/IP Client │──── 主动发起连接 ────► │ CrestronXsig  (TCP Server, 0.0.0.0)  │
│ XSIG 符号     │◄─── 长连接，双向帧 ───►│  join 缓存 + 回调分发                │
└───────────────┘                       └──────────────────────────────────────┘
```

两条硬约束，决定了上层几乎所有设计：

1. **HA 是 TCP 服务端，控制系统是客户端**。HA 不需要知道对端 IP；对端断线自动重连。同一时刻只维护一条活动连接。
2. **协议只有推送，没有查询**。控制系统按变化推送 join；协议里唯一的“要状态”指令是全量的 `0xFD`，**不存在“读某一根 join”**。因此 HA 不轮询（所有实体 `_attr_should_poll = False`），一根从未被推送过的 join 就是**未知**，不是 0/False/""。

---

## 2. 分层与模块

自底向上四层，依赖单向向下，**不允许反向依赖**：

| 层 | 文件 | 依赖 | 职责 |
|---|---|---|---|
| L1 协议 | `xsig_protocol.py` | 无（纯 stdlib） | 帧编解码：`FrameDecoder.feed()` / `encode_digital/analog/serial`。无 socket、无 asyncio、无 HA |
| L1 纯逻辑 | `value_coercion.py`、`join_commands.py`、`join_registry.py` | 无 HA | 模板值→join 值的强制转换；数字量“置一清零”/点动脉冲/成对反馈判定；join 归属冲突检测 |
| L2 传输 | `crestron.py` (`CrestronXsig`) | asyncio | TCP server、join 内存缓存、回调注册与分发、可用性广播 |
| L3 桥接 | `bridge.py`、`__init__.py` (`CrestronHub`) | HA helpers | `to_joins` / `from_joins` 两个方向的数据桥；HA 生命周期粘合 |
| L4 平台 | `light/switch/climate/cover/sensor/binary_sensor/number/select/media_player.py` | HA + `entity.py`/`device.py`/`schema.py`/`unique_ids.py` | 实体语义、能力推导、乐观状态与反馈调和 |

辅助模块：`const.py`（配置键常量）、`schema.py`（join 号校验器）、`entity.py`（实体 mixin + 平台装配）、`device.py`（HA 设备分组）、`unique_ids.py`（全部 9 个平台的最终 unique_id 规则与重复检测）、`config_flow.py`（配置项 + 重同步选项）、`diagnostics.py`（诊断导出）。

**L1 与 HA 无关是有意为之**：`mypy.ini` 只对这四个纯模块做类型检查，测试也能在不安装 Home Assistant 的前提下直接加载它们（`tests/loader.py` 用合成包绕开会 `import homeassistant` 的真实 `__init__.py`）。`schema.py` 直接从 `xsig_protocol.py` 取 join 上限——配置校验不依赖传输层。

---

## 3. 线协议（`xsig_protocol.py`）

首字节高位掩码区分帧类型：

| 类型 | 长度 | 位布局 | join 范围 | 值域 |
|---|---|---|---|---|
| digital | 2 B | `10v jjjjj  0jjj jjjj` | 1–4096 | 0/1，**电平位取反**（协议里 1 = 关） |
| analog | 4 B | `11vv 0jjj  0jjj jjjj  0vvvvvvv 0vvvvvvv` | 1–1024 | 0–65535（高 2 位在首字节） |
| serial | 变长 | `1100 1jjj  0jjj jjjj  <UTF-8>  0xFF` | 1–1024 | UTF-8 **≤ 252 字节**（非字符数） |

控制字节：`0xFD`（HA → 控制系统，请求全量上报）、`0xFB`（控制系统 → HA，请求 HA 重发所有 `to_joins`）。join 号线上从 0 计，解析后 +1。

`FrameDecoder` 是**增量、有状态**的解析器：`feed(chunk) -> list[Frame]`，跨 TCP 分片重组，半帧留在缓冲区。它只在一种情况下抛 `ProtocolError`（调用方应断链）：串行帧的 `0xFF` 终止符在 `SERIAL_SCAN_LIMIT`(64 KiB) 内没出现，防止缓冲区无限增长。

`Frame.kind` 是一个封闭集合，L2 逐个分派：`digital` / `analog` / `serial` / `sync_all` / `bad_utf8`（串行体非法 UTF-8，记 warning）/ `unknown`（消费 2 字节继续，不断链）。

---

## 4. 传输层（`crestron.py::CrestronXsig`）

状态：三张 join 缓存字典（`_digital` / `_analog` / `_serial`）、`_writer`（当前活动连接）、`_available`、两组回调表。

**回调模型**

- `register_callback(cb, joins)`：只收列表内 join 的事件，并**自动追加 `available` 键**，所以任何订阅者都能感知连接变化。
- 回调键的字符串形式：`d<n>` / `a<n>` / `s<n>`，可用性用常量 `AVAILABLE_KEY = "available"`。这套 key 在 bridge、实体、YAML（`to_joins`/`from_joins`）里是同一套词汇表。
- `_dispatch()` **先快照回调集合再迭代**：回调是 `await` 的，期间可能有实体增删改动这些集合。
- `_safe_call()` 逐个回调 try/except：单个订阅者抛异常不会拖垮 TCP 读循环。

**读写**

- 读：`reader.read(4096)` → `FrameDecoder.feed()` → `_handle_frame()`（更新缓存 → 分发）。
- 写：`set_digital/set_analog/set_serial` 先校验 join 范围（越界只记 warning 并丢弃），再 `writer.write()`；无连接时记 info 后丢弃。写入不等待 `drain()`（TODO 记录了“无背压”这一已知取舍）。
- `batched_writes()` 上下文管理器把块内所有 `set_*` 合并成**一次** `writer.write()`。帧彼此独立且保序，拼接与逐个写在线上等价。全量 `sync_all` 用它，把几百次小写入压成一次；可重入，内层块并入最外层，异常也会 flush。
- `pulse_digital` 的落电平在 `finally` 里：保持窗口内被取消（重载、停机）否则会把 join 永久留高。

**连接生命周期**

```
accept ──► 发 0xFD ──► available=True ──► connect_callback()（全量下发 to_joins）
        ──► 读循环（帧 → 缓存 → 回调）
        ──► 断开：只有“仍是活动连接”的那条才清 _writer 并广播 available=False
```

新连接到达时若旧 `_writer` 仍在，会**替换并关闭旧连接**；旧连接的 `finally` 通过 `was_active = self._writer is writer` 判定自己已被接管，从而不会误把 `available` 打成 False。`_notify_available` 对相同值静默，做可用性去抖。

**同步耗时探针**：仅当 logger ≥ INFO 时启用；统计首帧延迟、帧数、跨度与 HA 侧分发耗时，静默 `SYNC_SETTLE_SECONDS`(1 s) 后打一行汇总。看门狗以 `stats["last"]`（分发**完成**后写入）为准而不是 `first`，避免冷启动首帧分发超时导致 `monotonic() - None`。

**语义关键点**：`has_digital/has_analog/has_serial` 与 `get_*(default=…)` 成对存在，用于区分“未上报”和“上报为 0/False/空串”。`sensor` / `binary_sensor` / `cover` 依赖这一区分返回 `None`（unknown），以免把编造的数据写进 recorder 长期统计。

---

## 5. 桥接层（`__init__.py` + `bridge.py`）

`CrestronHub` 只做 HA 生命周期：持有 `CrestronXsig`、起停 server、组合两个 bridge、汇总 diagnostics。数据流实现全在 `bridge.py`。

**`ToJoinBridge`（HA → 控制系统）**

每条 `to_joins` 统一归一成一个 HA `Template`，优先级：`value_template` > `state_attr(entity_id, attribute)` > `states(entity_id)`。用 `async_track_template_result` 订阅；结果变化 → `resolve_join_write(key, result)` 强制转换 → 调对应 `hub.set_*`。

`resolve_join_write`（`value_coercion.py`，纯函数）规则：
- `d`：`on/true/1` → True，`off/false/0` → False，其余 → `None`（不写）。
- `a`：`int(float(s))` 后钳到 0–65535；`unknown/unavailable/None/""` → 不写。
- `s`：`str()`；同样过滤 unknown/unavailable。
- 返回 `None` 一律表示“**这次不写**”，让未知状态永不伪装成 0。

`sync_all()` 全量重渲染并下发，触发点有三个：控制系统发 `0xFB`、**新连接建立**（`connect_callback`）、配置项选项里手动勾选。第二个是必需的——`0xFD` 只让对端上报**它的** join，协议没有反方向的“把你的输出推给我”，不主动补发就会让面板反馈长期停在旧值。单条模板异常被隔离，不影响其余 join。

**`FromJoinBridge`（控制系统 → HA）**

按 join key 建 `Script`，注册精确 join 回调。脚本以 `hass.async_create_task` 后台执行，慢脚本不阻塞 XSIG 读循环；脚本异常被捕获记录。重复的 join key 会告警（后者仍然覆盖前者）。

数字 join **只在真正的 `0→1` 跳变时触发**，靠 `_last_digital` 记录每根 join 的前值。仅判断 `value != "0"` 不是边沿检测：`0xFD` 会让对端把**每一根** join 的当前电平报一遍，于是每次连接（HA 重启、主控重启、TCP 断线重连）所有恰好为高的按键 join 都会执行脚本——场景被重放。因此**前值未知一律不算边沿**，连接后的首次上报只建立基线；代价是与连接同一瞬间的按键会漏掉一次，这是刻意选的安全方向。

基线必须跟着**每一次连接**清空。这件事由 hub 的 **connect 回调**驱动（`CrestronHub._on_connect` → `FromJoinBridge.reset_connection_baseline()`），而不是 availability：availability 回答的是「现在在不在线」且会去重，**新连接接管一条尚未关完的旧连接时它根本不变**——新连接的 `_notify_available(True)` 被去重，旧连接的 `finally` 又发现自己已不是 active writer 而保持沉默，两头都不发事件，陈旧基线原样留下。connect 回调在读循环开始前执行，顺序上正好。断线（`available=False`）时也顺手清一次作为兜底。

---

## 6. 配置装载路径

配置**只来自 YAML**，config flow 不编辑任何参数——它存在的唯一理由是：HA 只给「配置项式」集成建**设备**，老式 `light: - platform: crestron` 拿不到 `device_info`。

```
configuration.yaml: crestron: !include crestron.yaml
        │
        ▼ async_setup()
   校验 CONFIG_SCHEMA（extra=ALLOW_EXTRA）→ _warn_unknown_config_keys()
                                        → _warn_join_conflicts()
   hass.data[crestron][yaml_config] = 配置块
   发起 SOURCE_IMPORT 配置流
        │
        ▼ async_setup_entry()
   CrestronHub(...).start()         ← 建 bridge、listen(port)
   hass.data[crestron][hub_wrapper] = hub;  hass.data[crestron][hub] = CrestronXsig
   async_forward_entry_setups(PLATFORMS)
        │
        ▼ 各平台 async_setup_entry()
   setup_platform_entities(hass, "<platform>", PLATFORM_SCHEMA, factory)
```

- 域级 schema 必须 `ALLOW_EXTRA`（实体定义在平台键下、由各平台自校验）。`_warn_unknown_config_keys()` 用白名单返回并记录未知键：首次启动只 warning，reload 遇到未知键直接拒绝并保留旧配置，避免 `lights:` 之类的拼写错误卸载整个平台。
- `setup_platform_entities()` **逐条**校验+构造实体，单条失败只跳过该条并记 warning，不会让整个平台空掉。
- `EVENT_HOMEASSISTANT_STOP` 与 `async_unload_entry` 都会走 `hub.stop()`：**先停 bridge（摘回调/模板追踪），再停 server**。

**重新加载**：`async_setup` 只在 HA 启动时读一次 YAML，所以单纯重载配置项会重放 `hass.data` 里的旧副本。`crestron.reload` 服务补上这一步——用 `async_integration_yaml_config()` 重读 configuration.yaml，先检查未知顶层键、平台 section 结构、逐条实体、join 冲突和重复 ID，再覆盖 `YAML_CONF` 并逐个 `async_reload(entry_id)`。**重读失败、缺少 `crestron:`、未知顶层键或整个 section 结构错误时均保留旧配置**；单条坏实体仍与启动一致，只跳过该条。改完（或用转换器重新生成）`crestron.yaml` 后调这个服务即可，不必重启 HA。

---

## 7. 平台实体的统一形态

`entity.py::CrestronEntity` 是 **mixin 而非基类 Entity**，必须写在真实 HA 实体类之前（`class X(CrestronEntity, LightEntity)`），以便其 `available` / `process_callback` 生效；子类 `__init__` 负责设置 `self._hub`。

它统一了五件事：不轮询、`available` 跟随 `hub.is_available()`、`async_added_to_hass` 按 `_callback_joins()` 注册回调、`async_will_remove_from_hass` 摘回调、以及**状态写合并**。

**写合并（`_schedule_write`）**：控制系统每根 join 单独成帧，所以一次真实变更会让订阅了 N 根 join 的实体被回调 N 次——一台订阅 12 根 join 的空调在冷启动全量同步时会写 12 次状态，而每次写都要构造 State、发事件、进 recorder。判定很便宜，写很贵。因此回调**立即**完成判定（缓存值始终最新）、只把实体标脏，真正的 `async_write_ha_state()` 由 `hass.loop.call_soon` 在当前这批帧处理完后执行一次。冷启动的状态写次数从「join 数」降到「实体数」。

只有**反馈驱动**的写被合并；命令路径（`async_turn_on` 等）仍直接写，因为那里的全部意义就是零延迟显示乐观状态。实体被移除时挂起的 flush 会被取消（移除后写状态会抛异常）。

两种用法：

- **无状态型**（binary_sensor、sensor、media_player、调光灯）：只覆写 `_callback_joins()`，默认 `process_callback` 调 `_schedule_write()`，取值全部走 `hub.get_*` 现算。
- **乐观型**（switch、只开关灯、cover、climate、number、select）：额外继承 `RestoreEntity`，覆写 `async_added_to_hass`（先 `await super()` 注册，再恢复上次状态，若已连接则用确定的反馈覆盖）与 `process_callback`（把反馈调和进本地缓存）。

**乐观状态 + 反馈调和**是本项目最核心的行为模式，规则一致：

1. 下命令 → 立即写本地乐观状态并 `async_write_ha_state()`（避免开关回弹）；
2. 收到反馈 → 只有反馈**确定**时才覆盖乐观值；
3. **成对命令 join 即反馈**：CP4N 在 `on_join`/`off_join`（或 `open_join`/`close_join`）上回传互斥电平。恰好一根为高 = 确定；两根同高（切换瞬间）或同低（尚未上报）= 不确定，保持现值。这条规则由 `join_commands.paired_feedback()` 统一实现，switch / 只开关灯 / climate 电源 / cover 开关四处共用；cover 额外要求两根都配置（单独一根说明不了位置）。
4. 冷启动窗口由 `RestoreEntity` 兜底——但那只是“HA 记得的上一次”，不等于真实状态。

各平台的特有约定（这些是与控制系统程序对齐的硬约定，改动前需确认 SIMPL 侧）：

| 平台 | 要点 |
|---|---|
| `light` | 能力由**已配 join** 决定而非 `type` 字段：有 `brightness_join` → 调光灯（0–65535 ↔ HA 0–255，非零亮度不四舍五入到 0）；否则 → `ColorMode.ONOFF` 的继电器灯。色温按 **K 值原样**写模拟 join（2700–6500）。**关灯是两步**：先原样重发当前电平 → `sleep(0.2 s)` → 写 0，凑出控制系统能识别的“高→0”跳变；`_command_seq` 用于在延时期间被再次开灯时取消那个 0。这段是模拟量时序，**不能**并进 `pulse_digital` |
| `switch` | 三种合法组合由 `_require_writable_join` 强制：仅 `switch_join`（直写电平）/ `on+off(+state_join)`（各 0.2 s 脉冲）/ 二者不可混用。可选 `mode_joins` = `{标签: 数字join}`：任一为高即“开”并暴露对应 `mode`；只有全部已上报且全低才判“关/关闭”，部分同步时状态与属性都不下结论 |
| `climate` | 一台空调 = 一个实体 = 一台设备。电源用点动 `on/off_join`，**电源状态读这对 join 的回传**（模式 join 只说“是哪个模式”，因此关机后模式锁存不会误判为开机）。运行模式与风速用 `set_one_clear_others`（先清后置，观察者不会看到两根同高）。温度按**原值摄氏整数**读写，绝不跟随 HA 系统单位。室温变化 < `TEMP_REPORT_THRESHOLD`(0.5 °C) 直接丢弃，防止刷爆 recorder。命令后 `POWER_SETTLE_SECONDS`(2 s) 内忽略电源反馈，避免旧反馈把乐观状态打回去 |
| `cover` | `pos_join` 是 **0–100 直读直写**（不是 XSIG 满量程）。三级降级：真实位置反馈 > 命令推断的乐观位置（`assumed_state = True`，开/关/停按钮永不因反馈被禁用）> 旧式 `is_closed_join` 粗判。一旦收到真实位置，乐观值立即作废 |
| `number` | 模拟原值按整数直读直写；用 `has_analog()` 区分未上报与真实 `0`；schema 和命令均拒绝小数而不截断；只恢复当前 min/max 范围内的旧值 |
| `select` | `{选项: 数字join}` 的“置一清零”；全低（切换瞬间）保持上一个值 |
| `sensor` | `value_join`（可 `divisor`）与 `mode_joins` **恰好二选一**；未上报返回 `None` |
| `binary_sensor` | 未上报返回 `None`（unknown），绝不返回 False |
| `media_player` | 音量 0–65535 ↔ 0–1；`source_number_join` 写 0 视为关机，`_last_source_num` 记住上次输入用于开机；source 编号仅正整数/ASCII 十进制字符串，整表归一化后编号和显示名均不得重复 |

---

## 8. 身份与设备

- **unique_id 使用 0.4.0 的最终规则，发布后不再改变**，且全部 9 个平台都在 `unique_ids.py` 里生成。light/switch 取稳定控制 join，cover 取 `open_join`（pos-only 为回退），climate 取 `on_join`。
- 由一**组** join 派生的 ID（select 的 `options`、sensor 的 `mode_joins`）用**组内全部 join 升序拼接**（`crestron_select_512_513_514`）。只取最小 join 不唯一：`{507,508}` 与 `{507,510}` 会碰撞；完整集合同时避免碰撞和 YAML 书写顺序影响。增删组内 join 被视为换了一项实体定义，会产生新的 ID。
- **集成绝不自动迁移、删除或认领 entity registry 记录**。旧 ID 缺少足够信息，猜测对应关系可能把另一实体的 entity_id、历史和区域转错。开发版升级、配置删除或构成 ID 的 join 变化留下的 unavailable 实体，由用户在 HA 实体页面确认后手动删除。
- `device.py::device_info()`：所有平台都支持可选的 `device_id` / `device_name` / `suggested_area`，同 `device_id` 的实体归入同一 HA 设备（例如一台空调的若干实体）。`suggested_area` 只是新设备的建议，不覆盖用户手工设置。
- 模拟与数字是**独立的编号空间**（a1..a1024 与 d1..d4096 无关）。`entity.py::join_uid()` 因此给数字 join 加 `d` 前缀——混用会让两个实体撞 ID，HA 会静默丢弃后注册的那个。

---

## 9. 运维面

- `diagnostics.py` → `CrestronHub.diagnostics()`：连接状态 + 监听端口 + 对端 + 三张 join 缓存 + 各平台配置实体数 + `join_usage`（在用 join 数与冲突清单）。因为协议不能查询单根 join，“这根 join 主控到底报没报过”只能从这里判断——**不在缓存里 = 从未回传**，问题在 SIMPL 侧。**串行正文与对端地址默认脱敏**（保留 join 号与字符数），因为这个文件的用途就是外发排障；数字/模拟量只是电平和数值，原样保留。
- **join 归属冲突检测**（`join_registry.py`）：转换器只能查它自己生成的表，手写 YAML 和 `to_joins` 撞车完全不过检。启动与 reload 时各跑一次，判据是读/写之分——**两个写者**同占一根 join 才算冲突；`to_joins`/`from_joins` 里重复的 join 键更严重（bridge 按 join 建字典，后一条直接顶掉前一条，模板或脚本根本不会运行）；**读者与写者共用是正常的**（sensor 用 `mode_joins` 镜像空调运行模式、binary_sensor 盯着继电器），必须保持静默否则整个检查就只是噪音。实测对本项目 264 个实体 / 750 根 join 的真实配置零误报。
- `config_flow.py::CrestronOptionsFlow`：唯一的选项是勾选后触发 `resync_to_joins()`，不持久化任何配置。提交即关闭对话框（不勾选也关），避免出现无法退出的表单。
- 日志分层：`custom_components.crestron`（连接/同步/越界/丢弃，INFO）与 `custom_components.crestron.crestron.frames`（逐帧，DEBUG，非常吵）。帧日志一律用惰性 `%s` 格式化——冷启动会有上千帧突发。

---

## 10. 离线工具链（`tools/`）

- `xlsx_to_yaml.py`：**纯标准库**（自己用 `zipfile` + `ElementTree` 解 xlsx，自己拼 YAML），单文件、离线、不在 HA 内运行。sheet → builder 映射：`灯光`→`build_light`、`插座`→`build_outlet`、`窗帘`→`build_cover`、`空调`→`build_ac`；产出统一的 `crestron:` 域配置。
  - 类型判定**只看 join 能力**，`功能` 列仅备注 —— 与 `light.py::_make_light` 的判定口径一致。
  - 校验先于生成：`_row_errors()` 按 sheet 检查 join 格式/范围/必填/互斥；**有任何 error 就 `WorkbookValidationError` 中止，绝不写半份 YAML**；未知 cover 类型与重复 join 只 warning。`--check` 只校验不写。
  - `dedup_names()` 同平台重名追加 ` 2`/` 3`，并同步修正单实体设备的 `device_name`。
  - `SUPPORTED_COVER_TYPES` 必须与 `cover.py::_COVER_DEVICE_CLASSES` 保持同步（两处各有注释互指）。
- `deploy_to_ha.sh`：scp 上传组件（**先 stage 再删 `*.pyc` / `__pycache__` / `crestron.yaml`**，避免把运行时配置覆盖掉），可选生成并上传 YAML，最后 `ha core restart`。
- `update.sh`：不依赖 git，直接拉 GitHub 分支 tarball 覆盖 `custom_components/crestron` 与 `tools`，适配 HAOS 精简终端。`git-pull.sh` 是需要 git 的稀疏拉取版本。

---

## 11. 测试策略

`python3 -m unittest discover -s tests`（CI：`.github/workflows/tests.yml`，Python 3.11/3.12/3.13 + `mypy --config-file mypy.ini`）。

**不安装 Home Assistant 也能跑**，靠两条路子：

1. `tests/loader.py` 把纯逻辑模块挂到合成包 `crestron_under_test` 下单独加载，使 `schema.py` 的 `from .crestron import ...` 仍能解析，同时绕开会 `import homeassistant` 的真实 `__init__.py`。
2. 平台测试在 `sys.modules` 里塞 Home Assistant 最小替身，但**保留真实 voluptuous**（所以 `requirements-test.txt` 需要它）。

`test_xsig.py` 是主要的端到端层：真起 TCP server + ephemeral 端口，覆盖入站解析、字节流拆碎重组、出站序列化、越界裁剪、`0xFB`、精细回调过滤、回调异常隔离、可用性去抖；`test_connection_takeover.py` 进一步把真实 TCP、连接接管、`FromJoinBridge` 边沿判定和 Script 执行串成一条链。其余按模块一一对应（协议、强制转换、schema、bridge、join 命令、平台装配、各平台行为、最终 unique_id、写合并、join 冲突、reload 服务、xlsx 转换）。

---

## 12. 不变量清单（改代码前先读）

1. **L1 三个纯模块不得引入 `homeassistant` 或 socket**，否则 mypy 与半数测试立刻失效。
2. **未上报 ≠ 0/False/""**。新增读路径要用 `has_*` 显式区分，`None` 表示 unknown。
3. **写路径遇到不可解释的值必须放弃写入**，不得回退成 0。
4. **成对 join 的反馈判定**统一走 `paired_feedback()`，不要再手写第五份。
5. **乐观状态只能被确定的反馈覆盖**；命令后需要 settle 窗口的地方（climate 电源）不要省。
5b. **平台 schema 必须校验能力组合**，不能只逐字段校验——单字段全合法、组合起来无法控制的实体照样会被建出来。
6. **0.4.0 的 unique_id 规则是发布边界**：只能在 `unique_ids.py` 中生成；单控制 join 带信号空间前缀，组实体使用完整 join 集合升序拼接。发布后不得改变规则，也不得自动改写 registry 补救。
7. **反馈路径用 `_schedule_write()`，命令路径用 `async_write_ha_state()`**——把命令路径也改成合并会让按钮出现可感知的延迟。
8. **模拟/数字编号空间独立**，凡是把 join 号拼进标识符的地方都要带类型前缀。
9. **回调集合迭代前必须快照**，回调异常必须就地隔离。
10. **调光灯的关灯两步时序、`pulse_digital` 的 0.2 s、climate 的 2 s settle** 都是与控制系统程序对齐的经验值，不是随手常量。
11. **`cover.pos_join` 是 0–100，不是 0–65535**；只有它是这个例外。
12. **转换器有 error 时绝不写文件**；`SUPPORTED_COVER_TYPES` 与 `cover.py` 同步。
13. **驱动物理设备的写出必须有取消保护**（`finally` 落电平），**触发脚本的读入必须有边沿判定**（记前值）。这两条的失效模式都是「重启后房子自己动」。
14. 行为或约定发生变化时，**同步更新 `README.md`（按代码现状写）与 `TODO.md`**。
