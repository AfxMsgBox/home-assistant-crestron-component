# Home Assistant Crestron XSIG 集成

通过 Crestron 控制系统的 **XSIG（Intersystem Communication）** 符号，将 Crestron 系统的数字/模拟/串行 join 与 Home Assistant 实体双向打通。本仓库在原版基础上扩展了灯光色温、瞬动开关、瞬动窗帘等控制方式。

支持的实体类型：`light`（含只开关灯）、`climate`（空调）、`cover`、`switch`、`number`、`select`、`binary_sensor`、`sensor`、`media_player`。

当前版本 `0.3.0`。**实体与连接配置全部写在 YAML 里**；集成同时带一个极简的 config flow，只负责创建配置项（让实体能归属到 HA「设备」），不在 UI 里编辑任何参数。配置项的 ⋮ 菜单里还提供「选项」（手动重新下发所有 `to_joins`）和「下载诊断」（导出连接状态 + 全部 join 缓存），见「运维与排障」。

## 架构

```
┌──────────────────┐         TCP          ┌──────────────────────────────────┐
│ Crestron 控制系统 │  ◄─ XSIG 二进制帧 ─► │  Home Assistant (本集成)          │
│  ┌────────────┐  │                      │  ┌────────────────────────────┐  │
│  │ XSIG 符号  │◄─┼──────────────────────┼─►│ CrestronXsig (crestron.py)  │  │
│  └────────────┘  │                      │  │  socket + joins 状态字典     │  │
│  数字/模拟/串行  │                      │  │  回调分发                    │  │
│      joins       │                      │  │  ▲ 帧编解码 xsig_protocol.py │  │
└──────────────────┘                      │  └─────────────┬──────────────┘  │
                                          │                │                 │
                                          │  ┌─────────────▼──────────────┐  │
                                          │  │ CrestronHub (__init__.py)   │  │
                                          │  │  生命周期 + 组合两个 bridge  │  │
                                          │  │  ToJoinBridge   ┐            │  │
                                          │  │  FromJoinBridge ┘ bridge.py  │  │
                                          │  └─────────────┬──────────────┘  │
                                          │                │                 │
                                          │  ┌─────────────▼──────────────┐  │
                                          │  │ 平台实体 (light / climate…)  │  │
                                          │  │  公共 mixin: entity.py       │  │
                                          │  └────────────────────────────┘  │
                                          └──────────────────────────────────┘
```

- 所有 join 状态缓存在 `CrestronXsig` 内存字典中；任何 join 变更触发已注册的回调，实体据此更新状态（多根 join 的连续变更会合并成一次状态写，见「稳定性说明」）。
- 模块分工：`xsig_protocol.py` 纯帧编解码（无 socket / 无 HA），`crestron.py` 管 TCP 与缓存回调，`bridge.py` 管 `to_joins`/`from_joins` 两个方向的数据桥，`__init__.py` 只剩 HA 生命周期粘合。

### 连接关系

**HA 是 TCP 服务端，Crestron 控制系统主动发起连接**。HA 不需要知道控制系统的 IP，控制系统断网/重启后会自动重连。

```
Crestron (TCP Client)  ──发起连接──►  HA (TCP Server, 监听 port)
        ◄────────── 长连接，双向 XSIG 帧互传 ──────────►
```

生命周期：

1. HA 启动时 `CrestronXsig.listen(port)` 在 `0.0.0.0:<port>` 开 server，等待连接。
2. Crestron 侧的 TCP/IP Client 符号（IP=HA 主机，端口=HA 监听端口）`Connect` 信号置 1 后发起连接。
3. HA 收到连接立刻发 `0xFD`，要求控制系统全量上报所有 join 当前值（冷启动同步）。
4. 长连接期间双方按 XSIG 帧格式互发 join 变更。
5. 控制系统可随时发 `0xFB`，触发 HA 重新下发所有 `to_joins` 配置的值。
6. 连接断开时，所有实体的 `available` 变为 `False`。

## 核心逻辑

### XSIG 协议（`xsig_protocol.py`）

帧的编解码是一个不依赖 socket / 不依赖 Home Assistant 的纯模块（`FrameDecoder` + `encode_*`），`crestron.py` 只负责把收到的字节喂给它、把它产出的字节写回 socket。

按字节首位掩码区分三类帧：

| 类型 | 字节数 | 帧格式（位）                                           | join 编号范围 | 值范围            |
|------|--------|--------------------------------------------------------|---------------|-------------------|
| 数字 | 2      | `10v jjjjj  0jjj jjjj`（v=电平反相）                  | 1–4096        | 0/1               |
| 模拟 | 4      | `11vv 0jjj  0jjj jjjj  0vvv vvvv  0vvv vvvv`           | 1–1024        | 0–65535           |
| 串行 | 不定   | `1100 1jjj  0jjj jjjj  <UTF-8 bytes>  0xFF`            | 1–1024        | UTF-8 ≤ 252 字节  |

其中 `j` 为 join 编号位（高位在前，解析后 +1），`v` 为值位：数字帧的电平位取反（协议里 1 表示关），模拟帧的 16 位值由首字节 `vv`（高 2 位）+ 后两字节各 7 位拼成。

控制字符：
- `0xFD`（HA → 控制系统）：请求控制系统上报所有 join 当前值。
- `0xFB`（控制系统 → HA）：请求 HA 下发所有已配置的 `to_joins` 值。

> 串行长度按 **UTF-8 字节** 计算（不是字符数）：常见汉字一个 3 字节，因此 252 字节约等于 84 个汉字。超过时该帧会被丢弃、记一条 **info** 日志，不会污染连接（默认 `logger: default: warning` 下看不到这条，排查串行截断时要把 `custom_components.crestron` 调到 info）。
> Join 编号越界（`d>4096` / `a/s>1024`）的写入会在运行时被丢弃并打 warning；YAML schema 也会拒绝越界配置。

### Hub 与数据桥（`__init__.py` + `bridge.py`）

`CrestronHub`（`__init__.py`）只负责 HA 生命周期：持有 `CrestronXsig`、起停 TCP server、组合下面两个 bridge。两个方向的数据流实现都在 `bridge.py`：

1. `ToJoinBridge`：把 `to_joins` 中每条配置统一封装成 HA Template，通过 `async_track_template_result` 监听；模板结果变化即调用 `set_digital/set_analog/set_serial` 推给控制系统。控制系统发 `0xFB`（或从选项里手动触发）时会重新渲染并全量下发，单条模板出错只跳过该条。
2. `FromJoinBridge`：注册 join 变更回调，匹配 `from_joins` 配置并以 `value` 变量执行对应 HA `script`。数字 join 只在**真正的 `0→1` 跳变**时触发（记录每根 join 的前值），避免点动按钮触发两次；脚本在后台任务里跑，慢脚本不会阻塞 XSIG 读取。

### 平台实体

所有平台实体均：
- 在 `async_added_to_hass` 中向 hub 注册一个回调；任何 join 变更都让该实体重新渲染状态。反馈引起的状态写会合并到事件循环的下一轮统一执行一次（一台空调订阅十几根 join，冷启动全量同步时不会写十几次状态）；命令引起的写仍然立即执行，按钮不会有延迟。
- 通过 hub 的 `is_available()` 反映 TCP 连接状态。
- 写操作直接调 `hub.set_*`，按 XSIG 协议序列化下发。

模拟 join 的 0–65535 范围在各平台内部映射到对应业务单位（亮度 0–255、音量 0–1）。**两处例外**：窗帘位置由主控直接用 0–100 模拟量表示百分比（0=全关，100=全开）；`climate` 的温度按**原值摄氏整数**直读直写，不做任何换算。`sensor` 的 `divisor` 是可选的分压系数（如温度 ×10 的场景填 10），由配置决定，不是全局约定。

### 状态同步模型（推送式，不轮询）

HA 与快思聪主控（如 CP4N）之间是**事件驱动的推送式同步**：HA 始终以主控回传为准，**自身不轮询**，XSIG 协议里也**没有"查询单根 join"的指令**。

**启动 / 连接时序：**

```
HA 启动（监听端口，available=False，实体显示「不可用」）
     │
     ▼  主控连接进来
HA ──0xFD──► 主控              “把所有 join 当前值发来”
HA ◄═全部 join 快照═══ 主控     digital/analog/serial 逐个入缓存 → 触发回调刷新
available=True                 实体显示真实状态
     │
     ▼  运行中（持续）
主控 ──某 join 变化──► HA       面板按键 / 逻辑联动任何变化都主动推送（事件驱动）
HA   ──命令──────────► 主控     开灯 / 设温等下发（含 to_joins 反馈）
     │
     ▼  断线重连
重连后 HA 再发一次 0xFD ── 重新全量同步
```

**为什么不查询：**
- 所有平台 `_attr_should_poll = False`，HA 不轮询实体。
- 读状态就是读 `get_digital/get_analog/get_serial`——它们只返回**被主控推送填充的内存缓存**，不发任何网络请求。
- 唯一一次"主动要状态"是连接建立时发 `0xFD`（**全量**快照）；协议没有"读某一根 join"的细粒度查询。

**⚠️ 前提与坑（排查必看）：**
- **同步是否正确，取决于主控程序是否为该 join 配了"状态回传"**。只接收命令、不回传状态的 command-only join，HA **永远无法**得知其真实状态——协议没有查询能力。本项目的开/关成对 Join 由 CP4N回传互斥状态，HA 同时读取两根：开高/关低=开，开低/关高=关，两根同高或同低时保持最近状态。
- **命令→回传之间有几十到数百毫秒的短暂不同步窗口**。集成用"乐观状态 + settle 窗口"过渡（先按命令乐观显示，回传到达后再以回传为准），避免按钮回弹。

> 本小节讲的是**实体状态同步**；把 HA 状态推到触摸屏/面板的反馈、或面板按键触发 HA 动作，见后文「控制面板同步（to_joins / from_joins）」一节。

## 安装

1. 复制 `custom_components/crestron/` 到 HA 配置目录的 `config/custom_components/` 下。
2. 在 `configuration.yaml` 配置 `crestron:` 块，**所有实体都写在 `crestron:` 之下**（见下文）。
3. 重启 Home Assistant。集成会自动从 YAML 创建一个**配置项（config entry）**，实体随之归属到对应**设备**下（例如一台空调=一个 `climate` 设备，灯/窗帘各自一个设备）。

> **为什么实体要写在 `crestron:` 之下、而不是顶层 `light:` / `switch:`？**
> Home Assistant 只为「配置项（config entry）式」的集成创建**设备**。老式 `light: - platform: crestron` 这种 YAML 平台写法没有配置项，HA 不会给它建设备（设备列永远是 `—`）。本集成把实体收在 `crestron:` 下、经配置项统一建立，才能让 `device_id` 把同一台设备的多个实体归到一起。

## 控制系统侧配置

1. 添加 **TCP/IP Client** 设备，IP 指向 HA 主机，端口与 HA `crestron.port` 一致。
2. 把 `Connect` 信号置 1（或接业务逻辑）。
3. 加入 **Intersystem Communication** 符号（快捷键 `xsig`），TX/RX 与 TCP/IP Client 互连。
4. 把要交换的数字/模拟/串行信号挂到 XSIG 输入/输出 join 上。

> **Join 编号陷阱**：同一 XSIG 上把模拟/串行与数字混挂时，数字 join 的实际编号是它在整个信号列表中的顺序位（例如 25 个模拟之后的第 1 个数字 = join 26）。推荐**用两个 XSIG 分别承载数字与模拟/串行**，让编号从 1 起。

## HA 配置

### 基础配置

所有实体都是 `crestron:` 下的**平台键列表**（`light:` / `switch:` / `number:` / `select:` / `sensor:` / `cover:` …）：

```yaml
crestron:
  port: 10200          # 必填，HA 监听端口
  to_joins:            # 可选：HA → 控制系统同步
    - ...
  from_joins:          # 可选：控制系统 → HA 触发脚本
    - ...
  light:               # 各平台实体直接列在 crestron: 之下
    - name: "射灯"
      type: brightness
      brightness_join: 1
    - name: "B2.车库 柜灯"       # 只开关的灯（无亮度）
      type: onoff
      on_join: 1
      off_join: 2
  climate:
    - name: "B2.洗衣房 空调"
      on_join: 505
      off_join: 506
      set_temp_join: 414
      reg_temp_join: 415
      mode_cool_join: 507
      fan_low_join: 512
      device_id: ac_505          # 同 id 的实体在 HA 里归到同一个设备
      device_name: "B2.洗衣房 空调"
```

> **下面各平台小节的示例可能沿用旧写法**（顶层平台键 + `- platform: crestron`），仅用于说明**字段**。新格式里请把它们**缩进到 `crestron:` 之下、并删掉 `- platform: crestron` 那一行**，其余字段照搬。整张表用 `tools/xlsx_to_yaml.py` 一键生成即可（见「批量生成配置」），无需手写。

平台与设备类型对应关系：

| Crestron 设备             | HA 平台          |
|---------------------------|------------------|
| 调光灯（模拟亮度+可选色温）| `light`          |
| 只开关的灯（继电器）      | `light`（仅开/关，无亮度） |
| 空调（电源/温度/风速 + 可切换运行模式） | `climate`   |
| 窗帘/卷帘                 | `cover`          |
| 多区域音频切换器          | `media_player`   |
| 只读数字 join             | `binary_sensor`  |
| 只读模拟 join             | `sensor`         |
| 可读写数字 join           | `switch`         |

### Light（灯光）

三种能力都用 `light` 平台，按"哪一列有 join"自动区分：

**双色温灯**：同时带亮度和色温（2700–6500 K，直接以 K 值写入模拟
join，需控制系统侧解析）。

```yaml
light:
  - name: "射灯"
    type: color_temp
    brightness_join: 1
    color_temp_join: 101
```

- `brightness_join`：模拟 join，0–65535 ↔ HA 0–255。
- `color_temp_join`：模拟 join，K 值直接读写（2700–6500）。

**单色温灯**：只有亮度能力。

```yaml
light:
  - name: "吊灯"
    type: brightness
    brightness_join: 2
```

**只开关的灯**（继电器/单一功能灯）：没有亮度，用点动 `on_join`/`off_join`。它在 HA 里**仍是一盏灯**（`ColorMode.ONOFF`，灯图标、语音"开灯"、归灯光类别），不是开关。状态从 `on_join`/`off_join` 两根命令 join 的回传电平读取（`on_join` 高=开、`off_join` 高=关），所以面板/外部开关后 HA 会跟着更新；两根都低（尚未回传）或都高（切换瞬间）时维持当前状态。下命令后先乐观显示并跨重启恢复（RestoreEntity）。

```yaml
light:
  - name: "B2.车库 柜灯"
    type: onoff
    on_join: 1
    off_join: 2
```

- `on_join`/`off_join`：点动开/关命令（各 0.2 秒脉冲），同时控制系统在这两根 join 上回传当前开/关状态。
- 可选 `state_join` 或 `switch_join`：配了就**优先**用作开关反馈（取代 on/off 回传）。

### 空调（climate）

每台空调 = **一个 `climate` 实体** = 一台设备、一张恒温器卡，并出现在 HA 的「空调」类别里。电源是**独立的开/关按钮**（点动 `on_join`/`off_join`）；**运行模式（制冷/制热/通风/除湿）是真实、可切换的模式**——more-info 的「模式」下拉里选哪个模式就置位对应的 mode join（选一置位、清其余），并随控制系统的反馈联动；选「关闭」即关机。温度设定与风速也可调。

**电源状态同时读取 `on_join`/`off_join` 的反馈电平**：开高/关低=开，开低/关高=关，两根同高或同低时保留最近状态。即使关机后某个 mode join 仍锁存为高，也不会误判成开机。模式 join 只决定「是哪个模式」；由于没有独立的压缩机运行反馈，组件不伪造 `hvac_action`。模式、温度和风速均由反馈 Join 实时校正；命令后先乐观显示并跨重启恢复。

```yaml
climate:
  - name: "B2.洗衣房 空调"
    on_join: 505              # 必填：电源开（脉冲）
    off_join: 506             # 必填：电源关（脉冲）
    set_temp_join: 414        # 可选：温度设定（模拟，原值摄氏）
    reg_temp_join: 415        # 可选：当前室温（模拟，原值摄氏）
    # 可切换的运行模式（数字量 join，选一置位、清其余；随反馈联动）
    mode_cool_join: 507       # 制冷 -> HVACMode.COOL
    mode_heat_join: 508       # 制热 -> HVACMode.HEAT
    mode_fan_join: 510        # 通风 -> HVACMode.FAN_ONLY
    mode_dry_join: 511        # 除湿 -> HVACMode.DRY
    # 风速（四档数字量 join，选一置位、清其余）
    fan_low_join: 512        # 低速
    fan_med_join: 513        # 中速
    fan_high_join: 514       # 高速
    fan_auto_join: 515       # 自动风速
    device_id: ac_505
    device_name: "B2.洗衣房 空调"
```

- 温度全程按**原值摄氏整数**（不做单位换算，避免被当华氏换算成 -3.9°C）。可设范围固定 16–30 °C，步进 1。
- 「模式」下拉显示 `关闭/制冷/制热/通风/除湿`（只列出配了 join 的模式）。选某模式若当前关机，会先脉冲 `on_join` 开机再置位该模式。
- 不配任何 mode join 时退化为 `关闭/自动` 的纯开关（`自动` 即开机）。
- 室温回传变化**小于 0.5 °C 会被丢弃**（不写入 HA 状态），避免主控频繁上报把 recorder 数据库刷爆。
- 这些条目由 `tools/xlsx_to_yaml.py` 从 join 表自动生成，见「批量生成配置」。

**设备分组**：`device_id`/`device_name` 这两个可选键**所有平台都支持**，相同 `device_id` 的实体归到同一个 HA 设备；不写 `device_name` 时用 `device_id` 当显示名，两个都不写则该实体不归属任何设备。生成器已自动给每盏灯/每个插座/每个窗帘/每台空调都填好了。

### Cover（窗帘/卷帘）

支持两类驱动：**模拟位置**（CSM-QMTDC 等）或**瞬动开/关**（普通电机+继电器）。

```yaml
# 模拟位置型
cover:
  - platform: crestron
    name: "客厅卷帘"
    type: shade
    pos_join: 26
    is_opening_join: 41
    is_closing_join: 42
    is_closed_join: 44
    stop_join: 43

# 瞬动开/关型
  - platform: crestron
    name: "茶室窗帘"
    type: curtain
    open_join: 15
    close_join: 16
    stop_join: 17
    is_closed_join: 18
```

- `type`：Home Assistant cover 设备类别，支持 `awning`、`blind`、`curtain`、
  `damper`、`door`、`garage`、`gate`、`shade`、`shutter`、`window`。空值或
  未知值按 `curtain` 处理。该字段影响图标、界面文字和语音语义，不改变 join
  的控制方式。
- `pos_join`：可选，模拟 join，**0–100** 直读直写（0=全关，100=全开），非 XSIG 0–65535 满量程。
  - 只要配置就提供百分比滑块；尚未收到位置反馈时用命令值乐观显示，并标记为假定状态。
  - CP4N开始回传位置后自动改用真实值，无需修改配置。
- `open_join` / `close_join`：可选数字 join，写入时自动 200 ms 高电平脉冲，同时承载 CP4N的互斥开/关反馈。
- `stop_join`：必填，停止脉冲。
- `is_opening/closing/closed_join`：可选反馈。
- 若同时配置 `open/close_join` 且无 `pos_join`，`set_position` 按 50% 阈值映射为开/关。
- **无 `pos_join`（瞬动开/关型）时为「假定状态」**（`assumed_state`）：卡片显示常驻可按的开/关/停按钮，开/关命令始终无条件下发，不会因反馈状态而禁用按钮。

### Switch（开关）

支持两种工作模式：

```yaml
# 模式 A：单 join 直接置位
switch:
  - platform: crestron
    name: "排气扇"
    switch_join: 65

# 模式 B：瞬动开/关 + 可选独立状态反馈
  - platform: crestron
    name: "灯组 1"
    on_join: 1          # 触发开（200 ms 脉冲）
    off_join: 2         # 触发关（200 ms 脉冲）
    state_join: 50      # 可选，纯瞬动模式建议配置
```

合法组合（schema 会拒绝其他写法）：

| 配置 | 写入 | 状态读取 |
|---|---|---|
| 仅 `switch_join` | 直写电平 | 同 `switch_join` |
| `on_join` + `off_join` + `state_join` | 各自 200 ms 脉冲 | `state_join` |
| `on_join` + `off_join`（无 `state_join`） | 各自 200 ms 脉冲 | 优先用 `on_join`/`off_join` 上的回传电平（恰好一根为高才算数）；两根都低或都高时保持当前状态 |

不允许：
- 单独 `on_join` 或单独 `off_join`（必须成对）
- `switch_join` 与 `on_join`/`off_join` 混用（脉冲模式下要反馈请用 `state_join`）

纯瞬动模式下开关是 `RestoreEntity`：**重启会恢复重启前的状态**，但那只是「HA 记得的上一次」，期间被面板改过就会不准。要真正可靠仍建议补 `state_join`，或让主控把状态回传到 `on_join`/`off_join` 上。

**可选 `mode_joins`（多态反馈）**：一组 `{标签: 数字join}`，任一为高即视为「开」，并把命中的标签作为只读属性 `mode` 暴露出来（都为低时 `mode` 为 `关闭`）。配了 `mode_joins` 后状态只看这组 join。

```yaml
switch:
  - name: "客厅空调电源"
    on_join: 505
    off_join: 506
    mode_joins:
      制冷: 507
      制热: 508
      通风: 510
      除湿: 511
```

> 空调建议直接用 `climate` 平台（一张恒温器卡搞定），`mode_joins` 主要留给不想建 climate 的简单场景。

### Binary Sensor（只读数字）

```yaml
binary_sensor:
  - platform: crestron
    name: "空压机"
    is_on_join: 57
    device_class: power
```

### Sensor（只读模拟 / 只读多态）

`value_join` 与 `mode_joins` **必须二选一**（都写或都不写都会被 schema 拒绝）。

**数值型**（模拟 join）：

```yaml
sensor:
  - platform: crestron
    name: "室外温度"
    value_join: 1
    device_class: temperature
    unit_of_measurement: "°C"
    state_class: measurement   # 可选，启用长期统计
    divisor: 10                # 模拟值除以 10 得到工程值
```

`divisor` 常用：温度 ×10 → 10；百分比 → 655.35。可选填 `state_class`（`measurement` / `total` / `total_increasing`）以接入 HA 长期统计。

> `unit_of_measurement` 要写 HA 认得的单位符号。带 `device_class: temperature` 时**必须**是 `°C` / `°F` / `K`——写成 `"C"` / `"F"` 会被 HA 拒绝，实体报错。

**多态文本型**（一组数字 join，报出为高的那个标签）：

```yaml
sensor:
  - name: "主卧空调运行模式"
    mode_joins:
      制冷: 507
      制热: 508
      通风: 510
      除湿: 511
```

全部为低时值为 `关闭`。

### Number（可读写模拟）

把一根模拟 join 暴露成可拖的数值框（典型用途：不想建 climate 时单独调温度设定）。

```yaml
number:
  - name: "主卧 温度设定"
    value_join: 414
    min: 16          # 可选，默认 16
    max: 30          # 可选，默认 30
    step: 1          # 可选，默认 1
    device_class: temperature      # 可选
    unit_of_measurement: "°C"      # 可选
```

- 值直读直写模拟 join 的**原值**（不做 0–65535 换算，也没有 `divisor`）。
- 回传值 `0` 一律当作「主控还没报过」忽略，避免开机瞬间显示成 0（低于 `min`）。
- 是 `RestoreEntity`：未连接时先显示重启前的值，连上后以回传为准。

### Select（可读写多态）

一组 `{选项名: 数字join}`，选中即「置一清零」——置位选中的那根、清掉同组其余的（和空调风速同一套逻辑）。

```yaml
select:
  - name: "主卧空调 风速"
    options:
      低速: 512
      中速: 513
      高速: 514
      自动: 515
```

- 当前选项从「哪一根为高」读回；全部为低（切换瞬间）时保持上一个值，不闪成空。
- 是 `RestoreEntity`：未连接时恢复重启前的选项。

### Media Player（多区域音频）

适用于 PAD-8A 等多区域切换器，每路输出建一个实体。

```yaml
media_player:
  - platform: crestron
    name: "厨房音箱"
    mute_join: 27
    volume_join: 19
    source_number_join: 13
    sources:
      1: "Android TV"
      2: "Roku"
      3: "Apple TV"
      7: "Volumio"
```

- `mute_join`：数字，True=静音（非 toggle，需控制系统侧直绑）。
- `volume_join`：模拟，0–65535 ↔ HA 0–1。
- `source_number_join`：模拟，写 0 即视为关机。
- `sources`：`输入编号: 显示名` 映射。

## 控制面板同步（to_joins / from_joins）

把 HA 状态推到 Crestron 触摸屏/按键面板的反馈 join，或在面板按下时触发 HA 脚本。

Join 写法：`d<N>` 数字、`a<N>` 模拟、`s<N>` 串行。Key 在配置加载时即校验格式与范围（`d1`–`d4096` / `a1`–`a1024` / `s1`–`s1024`），写错（如 `x1`、`d99999`、`a0`）会在 HA 启动期报错而不是运行期静默丢弃。

### HA → 控制系统（`to_joins`）

```yaml
crestron:
  port: 10200
  to_joins:
    - join: d12
      entity_id: switch.compressor
    - join: a35
      value_template: "{{ value | int * 10 }}"
    - join: s4
      value_template: "当前天气：{{ states('weather.home') }}"
    - join: a2
      entity_id: media_player.kitchen
      attribute: volume_level
```

三种写法二选一：
- `entity_id`：以实体 state 作为值。
- `entity_id` + `attribute`：以实体属性作为值。
- `value_template`：任意 [HA 模板](https://www.home-assistant.io/docs/configuration/templating/)，可引用多个实体。

数字 join 接受 `on/off/True/False`；模拟 join 自动 `int()`；串行 join 自动 `str()`。

### 控制系统 → HA（`from_joins`）

```yaml
crestron:
  port: 10200
  from_joins:
    - join: a2
      script:
        service: input_text.set_value
        data:
          entity_id: input_text.master_br_temp
          value: "主卧温度 {{ value | int / 10 }}"
    - join: d35
      script:
        service: media_player.media_previous_track
        data:
          entity_id: media_player.volumio
```

`script` 段为标准 [HA Script](https://www.home-assistant.io/docs/scripts/) 语法。脚本上下文中 `value` 变量即该 join 的当前值。数字 join 仅在 `0→1` 上升沿触发（按钮点动只会执行一次）。

> **「首次看到高电平」不算上升沿。** 主控连上来时会响应 `0xFD` 把**所有** join 的当前电平报一遍。如果把这次上报当成按键，那么每次 HA 重启、主控重启或断线重连，所有当时恰好为高的按键 join 都会执行一次脚本——场景被重放、灯被打开。因此集成要求先看到过 `0`、再看到 `1` 才触发；连接后的第一次上报只用来建立基线。
>
> 代价是：如果按键**恰好在连接建立的同一瞬间**被按下，这一次会被忽略。这是刻意选的方向——宁可漏一次按键，不可在重启时误动作。
>
> 模拟 / 串行 join 没有这个概念，每次上报都会触发。

## 批量生成配置（xlsx → YAML）

`tools/xlsx_to_yaml.py` 把一份多 sheet 的快思聪 join 表（Excel）转成**一个 `crestron.yaml`**（`crestron:` 域配置：port + 各平台实体），省去手写上百条实体。**纯标准库、单个自包含文件，需 Python 3.6+**，无需 openpyxl/PyYAML。它是**离线一次性工具，不在 HA 内运行**——在任意有 Python 3 的机器上跑即可，跟你的 HA 用哪个 Python 无关。

从零创建 Excel 的完整表头、列说明和示例见
[tools/README.md](tools/README.md)。

```bash
# 在任意装了 Python 3 的电脑上执行；只需要这一个 .py 文件，不必克隆整个仓库
python3 tools/xlsx_to_yaml.py <join表.xlsx> <输出目录或.yaml文件>
```

第二个参数既可以是目录（脚本会在其中生成 `crestron.yaml`），也可以直接写完整的
`.yaml` / `.yml` 文件路径。已有目标文件会被覆盖。

> 说明：HA 全系只用 Python 3（当前要求 3.12+），所以"不依赖 Python 3"在 HA 环境里是个伪命题——脚本本就用 Python 3，而各安装方式自带的 Python 都满足 3.6+。

#### 在 Home Assistant 主机上运行（以 HAOS 为例）

HAOS 自带的 `homeassistant` 容器跑的就是 Python 3.13，所以无需额外安装，按推荐度从高到低有三条路：

1. **最省事（推荐）：在自己电脑上跑，再把 yaml 拷进 `/config`。**
   在任意 Windows/Mac/Linux 上 `python3 xlsx_to_yaml.py 你的表.xlsx 输出目录`，把生成的 `crestron.yaml` 通过 Samba / 「File editor」/ 「Studio Code Server」放进 HA 配置目录（与 `configuration.yaml` 同级）。完全绕开"主机上有没有 Python"的问题。

2. **在 HAOS 上跑、复用 HA 自带的 Python（无需安装任何东西）：**
   - 装并打开「Advanced SSH & Web Terminal」加载项，把它的 **Protection mode 关掉**（这样终端里才有 `docker` 命令）。
   - 把 `xlsx_to_yaml.py` 和你的 `.xlsx` 上传到 `/config`。
   - 执行（复用 `homeassistant` 容器里的 Python 3，`/config` 在该容器内同路径可见）：
     ```bash
     docker exec -i homeassistant python3 /config/xlsx_to_yaml.py /config/你的表.xlsx /config
     ```
   - 生成的 `/config/crestron.yaml` 正好落在 HA 配置目录。

3. **或在 SSH 加载项里装一个 `python3`：**
   该加载项是精简 Alpine，默认没有 `python3`。在加载项「Configuration」的 `packages` 里加上 `python3`（持久），或终端里一次性 `apk add python3`（重启加载项后失效，够用一次转换）。然后：
   ```bash
   python3 /config/xlsx_to_yaml.py /config/你的表.xlsx /config
   ```


工作簿约定每个 sheet 一类设备，全部并入同一个 `crestron.yaml` 的对应平台键。**列名必须逐字匹配**（脚本按表头文字取值，改名即失效）：

| sheet | 列（`*` = 必填，其余留空则该能力不生成） | 产出平台键 |
|-------|----|-----------|
| `灯光` | 序号 楼层 房间 名称 功能 亮度 色温 开 关 | `light:`（调光灯 + 只开关灯） |
| `插座` | 序号 楼层 房间 名称 `开`* `关`* | `switch:`（`device_class: outlet`） |
| `窗帘` | 序号 楼层 房间 名称 类型 `开`* `关`* `停止`* 位置 | `cover:` |
| `空调` | 序号 楼层 房间 `开`* `关`* 制冷 制热 通风 除湿 低速 中速 高速 自动 温度 室温 风速值 | `climate:` |

- **灯光按 join 能力确定类型**：`亮度 + 色温` → `type: color_temp`
  （双色温）；仅 `亮度` → `type: brightness`（单色温）；仅完整的 `开 + 关`
  → `type: onoff`（只开关）。混合模拟量与数字量控制、只有色温、开关不成对，
  或没有任何能力的行都会跳过。`功能` 列只是给人看的备注，脚本不读。
- **有任何 error 就不写文件**：被跳过的行只是「这行不生成实体」；而 Join 格式/范围
  错误、必填 Join 缺失这类 **error 会中止整个生成**，不会写出半份 YAML，也不会覆盖
  已有文件。警告（未知窗帘类型、重复 Join）不阻止生成。先用 `--check` 看清单。
- **插座**：`开`/`关` 两根数字 join 出 `switch:`，主控在这两根 join 上回传通电状态。
- **窗帘**：`类型` 列支持 `awning`、`blind`、`curtain`、`damper`、`door`、
  `garage`、`gate`、`shade`、`shutter`、`window`（忽略大小写和首尾空格）；
  空白或未知值一律生成 `type: curtain`，不再根据名称猜测。`位置` 列有值就
  生成 `pos_join`（0–100 直读直写）；留空则退回瞬动开/关型（假定状态，见
  Cover 一节）。
- **空调**：风速列名是 **`自动`**（不是「自动风速」），生成的字段才是 `fan_auto_join`。`风速值`（模拟量风速）列**有意忽略**——数字量四档已能完整表达风速，两路并存会互相打架。
- **每台空调一个 `climate` 实体**：电源开/关按钮、温度、风速可控，运行模式可切换（制冷/制热/通风/除湿，随反馈联动）；进 HA「空调」类别，一台一张恒温器卡。
- 实体名 = `楼层.房间 名称`（空调为 `楼层.房间 空调`），同一平台内重名自动追加 ` 2`/` 3`。
- `说明` sheet 会作为说明页正常忽略；其他未知 sheet 会产生警告。

把生成的 `crestron.yaml` 放到 HA 配置目录，在 `configuration.yaml` 里用一行引入；生成器输出顶部已带 `port: 10200` 占位，改成你的端口、有 `to_joins/from_joins` 也并进这个文件：

```yaml
crestron: !include crestron.yaml
```

重启 HA，集成会从该 YAML 导入一个**配置项**并建立设备；灯 / 插座 / 窗帘 / 空调各自一个设备，每台空调是一张恒温器卡。

> 升级提示：把实体全部挪到 `crestron:` 之下、删掉 `- platform: crestron` 行即可（用本工具重新生成最省事）。当前 Light、Switch、Cover、Climate 的 `unique_id` 均由稳定控制 Join 决定；组件会自动迁移本集成先前按名称或可选反馈 Join 生成的 ID，保留实体 ID、历史和仪表盘引用。更早版本中跨平台改型留下的不可用实体仍需在“设置 → 实体”中人工确认后删除。

## 稳定性说明

- **HA 是 server**：Crestron 主动连，断网/重启会自动重连，HA 不需要配置目标 IP。
- **新连接接管**：旧 TCP 连接未正常关闭时，新连接到来会替换它，旧连接 finally 不再误置 `available=False`。
- **回调异常隔离**：单个 `from_joins` 脚本或实体回调抛异常不会拖垮 TCP 会话——错误会被记录并跳过，其他订阅照常工作。
- **可用性去抖**：`_notify_available` 在状态未变化时静默，避免实体反复刷新。
- **单实体隔离**：某条实体配置写错只会跳过那一条并记一条 warning，同平台其余实体照常加载。
- **乐观状态**：写出后立即更新 HA 本地状态，避免按钮回弹；随后以主控回传校正。带乐观状态的实体（switch / 只开关灯 / cover / climate / number / select）都是 `RestoreEntity`，重启会恢复重启前的值——但那只是「HA 记得的上一次」，不等于真实状态，能配反馈 join 就配。
  **调光灯和 media_player 没有乐观缓存**：它们的状态直接读主控回传的模拟量，所以从按下到界面更新要等一个回传往返（通常几十到几百毫秒）。调光灯**关灯**还要额外等 0.2 秒（见 Light 一节的两步时序），所以感觉上会慢半拍——这是为了让控制系统识别到电平跳变，不是卡顿。
- **状态写合并**：主控每根 join 单独成帧，订阅多根 join 的实体本会被回调多次。判定每次都做（缓存始终最新），但真正的状态写合并到事件循环下一轮执行一次。冷启动全量同步时状态写次数从「join 数」降到「实体数」，recorder 压力显著下降。命令引起的写不受影响，仍然立即执行。
- **join 归属检查**：加载配置时会检查有没有两个「写者」占用同一根 join，以及 `to_joins`/`from_joins` 里有没有重复的 join 键（重复键会被静默丢弃，只有最后一条生效）。有问题会在日志里列出来，也会出现在诊断下载的 `join_usage` 里。**只读的镜像不算冲突**——用 `sensor` 的 `mode_joins` 去镜像空调运行模式、用 `binary_sensor` 盯着继电器，都是正常用法，不会报。

## 运维与排障

### 修改 YAML 后重新加载（无需重启 HA）

改完 `crestron.yaml`（或用转换器重新生成）后，调用服务：

```yaml
service: crestron.reload
```

也可以在**开发者工具 → 操作**里搜 `Crestron` 找到「重新加载」。它会重新读取 `configuration.yaml` 里的 `crestron:` 段并重载集成，几百个实体一次生效，不必重启整个 Home Assistant。**需要管理员权限**。

重新加载时会先把新配置整体过一遍：不合法的实体会列在日志里（和启动时一样逐条跳过，不阻止重载），join 归属冲突和重复 unique_id 也会一并报出。

> 重读失败（YAML 语法错、或整段 `crestron:` 不见了）时会保留当前正在运行的配置并在日志里记一条 error，不会把能用的配置清空。
>
> **新配置加载失败也会自动回滚**。最常见的是端口被占用：新配置的 `port` 绑不上 → 配置项起不来 → 集成回退到上一份能用的配置并记一条 error。所以一次失败的重载不会让你失去所有实体。
>
> 注意这是**重新加载配置**，不是重连主控：TCP server 会重启，主控随后自动重连。

### 下载诊断

集成配置项（设置 → 设备与服务 → Crestron XSIG）的 ⋮ 菜单 → **下载诊断**，会导出一个 JSON：

- `available` / `connected` / `peer` / `listening_port`：当前连接状态与对端地址。
- `configured_entities`：各平台配了多少个实体，以及 `to_joins` / `from_joins` 条数。
- `join_usage`：配置里一共用到多少根 join，以及检测到的归属冲突清单。
- `digital` / `analog` / `serial`：**主控迄今推送过的全部 join 及其值**。

> **串行 join 的正文和主控 IP 会被脱敏**（只保留 join 号和字符数）。串行 join 常用来推送门禁姓名、日程等文本，而这个文件的用途就是发给别人排障。判断「这根 join 报没报过」只需要 join 号，脱敏不影响排查。数字/模拟量只是电平和数值，原样保留。

因为协议没有「查询单根 join」的能力，"这根 join 主控到底报没报过"只能从这里看——**某个 join 压根不在这份缓存里，就说明主控从没回传过它**，问题在 SIMPL 侧而不是 HA 侧。

### 手动重新下发 to_joins

同一个 ⋮ 菜单 → **选项**，勾选「重新同步所有 to_joins 到主控」并提交，即把 HA 当前已知的所有 `to_joins` 值重新渲染并下发一遍。平时这只在主控重连或主控发 `0xFB` 时自动发生；面板反馈显示不对又不想重启主控时用它。这里不保存任何配置，配置仍然只来自 YAML。

### 日志

```yaml
logger:
  default: warning
  logs:
    custom_components.crestron: info          # 连接、同步耗时、越界与丢弃
    custom_components.crestron.crestron.frames: debug   # 逐帧收发（很吵）
```

调到 `info` 时，主控首次全量同步结束后会打一行汇总：同步了多少个 join、耗时多久、其中 HA 侧分发占多少。

### 更新集成

仓库 `tools/` 下另有两个脚本（跟 xlsx 转换无关）：

- `tools/update.sh [分支]`：从 GitHub 下载分支压缩包就地更新 `custom_components/crestron/` 与 `tools/`，**不依赖 git**（curl 或 wget 有一个就行），适合 HAOS「Advanced SSH & Web Terminal」这种精简环境。
- `tools/git-pull.sh <git地址> <分支> <子目录> [本地目录]`：需要 git 的稀疏拉取版本。

## 已知问题

- **测试套件缺 `voluptuous` 时会报错而不是跳过**：见下节。

完整清单与修复计划见 [TODO.md](TODO.md)。

## 测试

仓库自带一套 unittest 套件（`tests/`）。除少数几个纯函数模块外，多数测试通过在 `sys.modules` 里塞入 `homeassistant` 的最小替身来加载平台代码，**因此不需要安装 Home Assistant，也不需要 `pytest-homeassistant-custom-component`**：

```bash
python3 -m unittest discover -s tests
```

> ⚠️ 需要先装 `voluptuous`（`pip install voluptuous`）。没装的话 `test_schema.py` 会优雅跳过，但 `test_switch_feedback.py` / `test_dimmable_light.py` / `test_onoff_light.py` / `test_climate_filter.py` 会直接 `ModuleNotFoundError` 报错（9 个 error）。这是测试的问题，已记在 TODO.md。

覆盖范围：

| 文件 | 覆盖内容 |
|---|---|
| `test_xsig.py` | 真实 TCP server + ephemeral 端口跑端到端：帧入站解析、字节流拆碎重组、`set_*` 出站序列化、模拟越界裁剪、串行超长丢弃、`0xFB` 同步请求、按 join 精细回调过滤、回调异常隔离、可用性去抖与断连置不可用 |
| `test_xsig_protocol.py` | 纯 codec：`FrameDecoder` 增量解析与 `encode_*` 位布局 |
| `test_value_coercion.py` | 模板值 → XSIG 值转换（`unknown`/`unavailable`/`on/off`/数字字符串/越界裁剪） |
| `test_schema.py` | `join_key` 与数字 join 校验器的格式/范围边界 |
| `test_bridge.py` | `ToJoinBridge` / `FromJoinBridge`：模板追踪下发、`0xFB` 全量重发、单条失败隔离、数字 join 上升沿过滤 |
| `test_join_commands.py` | `pulse_digital` 脉冲时序与并发串行化、`set_one_clear_others` 先清后置 |
| `test_setup_platform_entities.py` | 单条实体配置出错只跳过该条 |
| `test_dimmable_light.py` | 关灯的「重发当前电平 → 延时 → 写 0」两步时序，以及延时期间又被开灯时取消那个 0 |
| `test_onoff_light.py` | 只开关灯从 `on_join`/`off_join` 回传电平判状态（恰好一根为高才算数） |
| `test_switch_feedback.py` | 瞬动 switch 订阅两根命令 join、外部开/关能反映、两根同高或同低时保持原状态 |
| `test_cover_position.py` | cover 位置乐观/真实反馈、开关反馈、`assumed_state` 与稳定滑块能力 |
| `test_climate_filter.py` | 室温 0.5 °C 上报阈值、成对电源反馈、settle 窗口与设备分组 |
| `test_unique_ids.py` | 各平台稳定 ID（含 select/sensor 组 ID 与 YAML 书写顺序无关）及旧实体注册表迁移 |
| `test_write_coalescing.py` | 一批 join 变更只写一次状态、后续批次照常写、移除时取消挂起的写 |
| `test_join_registry.py` | join 归属冲突：两个写者报冲突、只读镜像不报、重复 to_joins/from_joins 键、模拟/数字空间独立 |
| `test_reload.py` | `crestron.reload` 重读 YAML 并重载配置项；重读失败时保留旧配置 |
| `test_xlsx_to_yaml.py` | 行→实体映射（灯光→调光/只开关 light、插座→switch、空调→单个 climate、窗帘 type、重名去重、域配置输出） |
| `loader.py` | 把被测模块挂到合成包下单独加载，绕开会 `import homeassistant` 的真实 `__init__.py` |
