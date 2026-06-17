# Home Assistant Crestron XSIG 集成

通过 Crestron 控制系统的 **XSIG（Intersystem Communication）** 符号，将 Crestron 系统的数字/模拟/串行 join 与 Home Assistant 实体双向打通。本仓库在原版基础上扩展了灯光色温、瞬动开关、瞬动窗帘等控制方式。

支持的实体类型：`light`（含只开关灯）、`climate`（空调）、`cover`、`switch`、`number`、`select`、`binary_sensor`、`sensor`、`media_player`。

当前版本 `0.3.0`（YAML 配置，无 config flow）。

## 架构

```
┌──────────────────┐         TCP          ┌────────────────────────────┐
│ Crestron 控制系统 │  ◄─ XSIG 二进制帧 ─► │  Home Assistant (本集成)    │
│  ┌────────────┐  │                      │  ┌──────────────────────┐  │
│  │ XSIG 符号  │◄─┼──────────────────────┼─►│ CrestronXsig (TCP)    │  │
│  └────────────┘  │                      │  │  joins 状态字典        │  │
│  数字/模拟/串行  │                      │  │  回调分发              │  │
│      joins       │                      │  └──────────┬───────────┘  │
└──────────────────┘                      │             │              │
                                          │  ┌──────────▼───────────┐  │
                                          │  │ CrestronHub          │  │
                                          │  │  to_joins / from_joins│ │
                                          │  └──────────┬───────────┘  │
                                          │             │              │
                                          │  ┌──────────▼───────────┐  │
                                          │  │ 平台实体 (light等)    │  │
                                          │  └──────────────────────┘  │
                                          └────────────────────────────┘
```

- 所有 join 状态缓存在 `CrestronXsig` 内存字典中；任何 join 变更触发已注册的回调，实体收到后调用 `async_write_ha_state()`。

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

### XSIG 协议（`crestron.py`）

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

> 串行长度按 **UTF-8 字节** 计算（不是字符数）：常见汉字一个 3 字节，因此 252 字节约等于 84 个汉字。超过时该帧会被丢弃并打 warning，不会污染连接。  
> Join 编号越界（`d>4096` / `a/s>1024`）的写入会在运行时被丢弃；YAML schema 也会拒绝越界配置。

### Hub 与回调（`__init__.py`）

`CrestronHub` 包裹 `CrestronXsig`，承担两件事：
1. 把 `to_joins` 中每条配置统一封装成 HA Template，通过 `async_track_template_result` 监听；模板结果变化即调用 `set_digital/set_analog/set_serial` 推给控制系统。
2. 注册 join 变更回调，匹配 `from_joins` 配置并以 `value` 变量执行对应 HA `script`。数字 join 的 `1→0` 跳变会被忽略，避免点动按钮触发两次。

### 平台实体

所有平台实体均：
- 在 `async_added_to_hass` 中向 hub 注册一个回调，回调实现就是 `self.async_write_ha_state()`——任何 join 变更都让该实体重新渲染状态。
- 通过 hub 的 `is_available()` 反映 TCP 连接状态。
- 写操作直接调 `hub.set_*`，按 XSIG 协议序列化下发。

模拟 join 的 0–65535 范围在各平台内部映射到对应业务单位（亮度 0–255、位置 0–100、音量 0–1、温度 ×10 等）。

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

两种灯都用 `light` 平台，按"哪一列有 join"自动区分：

**调光灯**：带亮度,可选色温（2700–6500 K，直接以 K 值写入模拟 join，需控制系统侧解析）。

```yaml
light:
  - name: "射灯"
    type: brightness
    brightness_join: 1
    color_temp_join: 101   # 可选
```

- `brightness_join`：模拟 join，0–65535 ↔ HA 0–255。
- `color_temp_join`：可选模拟 join，K 值直接读写（2700–6500）。

**只开关的灯**（继电器/单一功能灯）：没有亮度，用点动 `on_join`/`off_join`。它在 HA 里**仍是一盏灯**（`ColorMode.ONOFF`，灯图标、语音"开灯"、归灯光类别），不是开关。状态从 `on_join`/`off_join` 两根命令 join 的回传电平读取（`on_join` 高=开、`off_join` 高=关），所以面板/外部开关后 HA 会跟着更新；两根都低（尚未回传）或都高（切换瞬间）时维持当前状态。下命令后先乐观显示并跨重启恢复（RestoreEntity）。

```yaml
light:
  - name: "B2.车库 柜灯"
    on_join: 1
    off_join: 2
```

- `on_join`/`off_join`：点动开/关命令（各 0.2 秒脉冲），同时控制系统在这两根 join 上回传当前开/关状态。
- 可选 `state_join` 或 `switch_join`：配了就**优先**用作开关反馈（取代 on/off 回传）。

### 空调（climate）

每台空调 = **一个 `climate` 实体** = 一台设备、一张恒温器卡，并出现在 HA 的「空调」类别里。电源是**独立的开/关按钮**（点动 `on_join`/`off_join`）；**运行模式（制冷/制热/通风/除湿）是真实、可切换的模式**——more-info 的「模式」下拉里选哪个模式就置位对应的 mode join（选一置位、清其余），并随控制系统的反馈联动；选「关闭」即关机。温度设定与风速也可调。

**电源状态读 `on_join` 的反馈电平**（开机回传 1、关机回传 0），这是开关的唯一真值——即使关机后某个 mode join 仍锁存为高，也不会误判成「开」。`on_join`/`off_join` 仍是点动命令，但控制系统会在 `on_join` 上持续回传电源状态。模式 join 只决定「是哪个模式」，不再参与开关判定。模式、温度设定、风速同样从反馈 join 实时校正；下达命令后会先乐观显示、防止回弹，状态也跨重启恢复（RestoreEntity）。

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

- 温度全程按**原值摄氏整数**（不做单位换算，避免被当华氏换算成 -3.9°C）。
- 「模式」下拉显示 `关闭/制冷/制热/通风/除湿`（只列出配了 join 的模式）。选某模式若当前关机，会先脉冲 `on_join` 开机再置位该模式。
- 不配任何 mode join 时退化为 `关闭/自动` 的纯开关（`自动` 即开机）。
- 这些条目由 `tools/xlsx_to_yaml.py` 从 join 表自动生成，见「批量生成配置」。

**设备分组**：`device_id`/`device_name` 这两个可选键任何平台都支持，相同 `device_id` 的实体归到同一个 HA 设备。生成器已自动给每盏灯/每个窗帘各建一个设备、每台空调一个设备。

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

- `type`：`shade`（卷帘）或 `curtain`（窗帘），影响设备类别。
- `pos_join`：可选，模拟 join，0=全关 65535=全开；存在时启用 `SET_POSITION` 能力。
- `open_join` / `close_join`：可选数字 join，写入时自动 200 ms 高电平脉冲。
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

三种合法组合（schema 会拒绝其他写法）：

| 配置 | 写入 | 状态读取 |
|---|---|---|
| 仅 `switch_join` | 直写电平 | 同 `switch_join` |
| `on_join` + `off_join`（+ 可选 `state_join`） | 各自 200 ms 脉冲 | `state_join` 优先；无 `state_join` 时用 HA 本地乐观状态 |

不允许：
- 单独 `on_join` 或单独 `off_join`（必须成对）
- `switch_join` 与 `on_join`/`off_join` 混用（脉冲模式下要反馈请用 `state_join`）

无反馈 join 的纯瞬动模式可以工作，但**重启或重载 HA 后真实状态会丢失**，强烈建议补 `state_join`。

### Binary Sensor（只读数字）

```yaml
binary_sensor:
  - platform: crestron
    name: "空压机"
    is_on_join: 57
    device_class: power
```

### Sensor（只读模拟）

```yaml
sensor:
  - platform: crestron
    name: "室外温度"
    value_join: 1
    device_class: temperature
    unit_of_measurement: "F"
    state_class: measurement   # 可选，启用长期统计
    divisor: 10                # 模拟值除以 10 得到工程值
```

`divisor` 常用：温度 ×10 → 10；百分比 → 655.35。可选填 `state_class`（`measurement` / `total` / `total_increasing`）以接入 HA 长期统计。

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

## 批量生成配置（xlsx → YAML）

`tools/xlsx_to_yaml.py` 把一份多 sheet 的快思聪 join 表（Excel）转成**一个 `crestron.yaml`**（`crestron:` 域配置：port + 各平台实体），省去手写上百条实体。纯标准库，无需 openpyxl/PyYAML。

```bash
python3 tools/xlsx_to_yaml.py <join表.xlsx> <输出目录>
```

工作簿约定每个 sheet 一类设备，全部并入同一个 `crestron.yaml` 的对应平台键：

| sheet | 列 | 产出平台键 |
|-------|----|-----------|
| `灯光` | 楼层 房间 名称 功能 亮度 色温 开 关 | `light:`（调光灯 + 只开关灯） |
| `空调` | 楼层 房间 开 关 制冷 制热 通风 除湿 低速 中速 高速 自动风速 温度 室温 | `climate:` |
| `窗帘` | 楼层 房间 名称 开 关 停止 | `cover:` |

- **灯光按"列是否有值"判断能力**：有 `亮度` → 调光 light（有 `色温` 再加色温）；只有 `开/关` → 只开关的 light（`ColorMode.ONOFF`）；空行/占位（如 `//`）自动跳过。
- **每台空调一个 `climate` 实体**：电源开/关按钮、温度、风速可控，运行模式可切换（制冷/制热/通风/除湿，随反馈联动）；进 HA「空调」类别,一台一张恒温器卡。
- 实体名 = `楼层.房间 名称`，同名自动追加 ` 2`/` 3`（兼容旧表头 `名字`）。

把生成的 `crestron.yaml` 放到 HA 配置目录，在 `configuration.yaml` 里用一行引入；生成器输出顶部已带 `port: 10200` 占位，改成你的端口、有 `to_joins/from_joins` 也并进这个文件：

```yaml
crestron: !include crestron.yaml
```

重启 HA，集成会从该 YAML 导入一个**配置项**并建立设备；每台空调是一个 `climate` 设备(恒温器卡),灯/窗帘各自一个设备。

> 升级提示：把实体全部挪到 `crestron:` 之下、删掉 `- platform: crestron` 行即可（用本工具重新生成最省事）。注意本版把**只开关的灯从 `switch.*` 改成了 `light.*`**、**空调从 `switch/number/select/sensor` 合并成单个 `climate.*`**，这些实体的 `unique_id` 变了，旧实体会变成不可用孤儿——重启后到 设置 → 实体 筛选「不可用」批量删一次即可；新实体照常按 `device_id` 归到设备下。

## 稳定性说明

- **HA 是 server**：Crestron 主动连，断网/重启会自动重连，HA 不需要配置目标 IP。
- **新连接接管**：旧 TCP 连接未正常关闭时，新连接到来会替换它，旧连接 finally 不再误置 `available=False`。
- **回调异常隔离**：单个 `from_joins` 脚本或实体回调抛异常不会拖垮 TCP 会话——错误会被记录并跳过，其他订阅照常工作。
- **可用性去抖**：`_notify_available` 在状态未变化时静默，避免实体反复刷新。
- **乐观状态**：纯瞬动开关（仅 `on_join + off_join`，无 `state_join`）写出后立即更新 HA 本地状态；**重启 HA 会丢失真实状态**，建议补 `state_join`。

## 测试

仓库自带一套 unittest 套件（`tests/`），覆盖不依赖 Home Assistant 运行时的纯逻辑模块。仅 `tests/test_schema.py` 需要 `voluptuous`（不装会自动跳过）：

```bash
python3 -m unittest discover -s tests
```

覆盖范围：

- `tests/test_xsig.py`：用真实 TCP server + ephemeral 端口跑 XSIG 协议端到端——digital/analog/serial 帧入站解析、字节流被拆碎重组、`set_*` 出站序列化、模拟越界裁剪、串行超长丢弃、`0xFB` 同步请求、按 join 精细回调过滤、回调异常隔离、可用性去抖与断连置不可用。
- `tests/test_value_coercion.py`：纯函数测试模板值→XSIG 值转换（`unknown`/`unavailable`/`on/off`/数字字符串/越界裁剪等）。
- `tests/test_schema.py`：`join_key` 与数字 join 校验器的格式/范围边界。
- `tests/test_xlsx_to_yaml.py`：`tools/xlsx_to_yaml.py` 的行→实体映射（灯光→调光/只开关 light、空调→单个 climate、窗帘 type、重名去重、域配置输出）。
- `tests/loader.py`：把上述模块挂到合成包下单独加载，绕开会 `import homeassistant` 的真实 `__init__.py`。

> 实体级测试（light / switch / cover / climate / media_player）需要 `pytest-homeassistant-custom-component`，本仓库未集成。
