# Home Assistant Crestron XSIG 集成

通过 Crestron 控制系统的 **XSIG（Intersystem Communication）** 符号，将 Crestron 系统的数字/模拟/串行 join 与 Home Assistant 实体双向打通。本仓库在原版基础上扩展了灯光色温、瞬动开关、瞬动窗帘等控制方式。

支持的实体类型：`light`、`climate`、`cover`、`switch`、`binary_sensor`、`sensor`、`media_player`。

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
2. 在 `configuration.yaml` 配置 `crestron:` 块及各平台（见下文）。
3. 重启 Home Assistant。

## 控制系统侧配置

1. 添加 **TCP/IP Client** 设备，IP 指向 HA 主机，端口与 HA `crestron.port` 一致。
2. 把 `Connect` 信号置 1（或接业务逻辑）。
3. 加入 **Intersystem Communication** 符号（快捷键 `xsig`），TX/RX 与 TCP/IP Client 互连。
4. 把要交换的数字/模拟/串行信号挂到 XSIG 输入/输出 join 上。

> **Join 编号陷阱**：同一 XSIG 上把模拟/串行与数字混挂时，数字 join 的实际编号是它在整个信号列表中的顺序位（例如 25 个模拟之后的第 1 个数字 = join 26）。推荐**用两个 XSIG 分别承载数字与模拟/串行**，让编号从 1 起。

## HA 配置

### 基础配置

```yaml
crestron:
  port: 10200          # 必填，HA 监听端口
  to_joins:            # 可选：HA → 控制系统同步
    - ...
  from_joins:          # 可选：控制系统 → HA 触发脚本
    - ...
```

平台与设备类型对应关系：

| Crestron 设备             | HA 平台          |
|---------------------------|------------------|
| 调光灯（模拟亮度+可选色温）| `light`          |
| 空调（开关/设定点/风速）  | `climate`        |
| 窗帘/卷帘                 | `cover`          |
| 多区域音频切换器          | `media_player`   |
| 只读数字 join             | `binary_sensor`  |
| 只读模拟 join             | `sensor`         |
| 可读写数字 join           | `switch`         |

### Light（灯光）

支持亮度控制，可选色温（1500–5000 K，直接以 K 值写入模拟 join，需控制系统侧解析）。

```yaml
light:
  - platform: crestron
    name: "射灯"
    type: brightness
    brightness_join: 1
    color_temp_join: 101   # 可选
```

- `brightness_join`：模拟 join，0–65535 ↔ HA 0–255。
- `color_temp_join`：可选模拟 join，K 值直接读写。

### Climate（空调）

面向「开关 + 单设定点 + 风速 + 当前温度」的空调：**不开放模式选择**，当前在制冷/制热/除湿/送风通过只读的 `hvac_action` 显示。温度按**原值整数**读写（26 = 26°C，不缩放）。

```yaml
climate:
  - platform: crestron
    name: "B2.洗衣房 空调"
    on_join: 505          # 数字：开机命令（200ms 脉冲）
    off_join: 506         # 数字：关机命令（200ms 脉冲）
    set_temp_join: 414    # 模拟：温度设定点（原值整数）
    reg_temp_join: 415    # 模拟：当前室温（原值整数）
    mode_cool_join: 507   # 可选：制冷 运行反馈
    mode_heat_join: 508   # 可选：制热 运行反馈
    mode_fan_join: 510    # 可选：通风 运行反馈
    mode_dry_join: 511    # 可选：除湿 运行反馈
    fan_low_join: 512     # 可选：低速
    fan_med_join: 513     # 可选：中速
    fan_high_join: 514    # 可选：高速
    fan_auto_join: 515    # 可选：自动
```

- `on_join` / `off_join`：必填，开/关机点动命令。
- `set_temp_join` / `reg_temp_join`：必填，设定点 / 当前温度（模拟，原值整数）。
- `mode_*_join`：可选，运行模式反馈。**任一为 1 即视为开机**（HA 据此判断 on/off）；同时用于显示 `hvac_action`（制冷/制热/除湿/送风）。
- `fan_*_join`：可选，风速反馈/选择（设选中、清其余）；配了任一即启用风速选择（low/medium/high/auto）。
- HA 模式只有 `off` / `auto`（auto 即"开机"），开关即通过它或电源开关完成。

> 字段名/编号可由 `tools/xlsx_to_yaml.py` 从快思聪 join 表（xlsx）自动生成，见「批量生成配置」。

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

`tools/xlsx_to_yaml.py` 把一份多 sheet 的快思聪 join 表（Excel）转成按 HA 平台分好的 YAML，省去手写上百条实体。纯标准库，无需 openpyxl/PyYAML。

```bash
python3 tools/xlsx_to_yaml.py <join表.xlsx> <输出目录>
```

工作簿约定每个 sheet 一类设备：

| sheet | 列 | 产出 |
|-------|----|------|
| `灯光` | 楼层 房间 名字 功能 亮度 色温 开 关 | `light.yaml` + `switch.yaml` |
| `空调` | 楼层 房间 开 关 制冷 制热 通风 除湿 低速 中速 高速 自动 温度 室温 | `climate.yaml` |
| `窗帘` | 楼层 房间 名称 开 关 停止 | `cover.yaml` |

- **灯光按"列是否有值"判断能力**：有 `亮度` → light（有 `色温` 再加色温）；只有 `开/关` → switch；空行/占位（如 `//`）自动跳过。
- 实体名 = `楼层.房间 名字`，同名自动追加 ` 2`/` 3`。
- 输出文件是实体**列表**，直接 `!include` 到对应平台键：

```yaml
light:   !include crestron/light.yaml
switch:  !include crestron/switch.yaml
cover:   !include crestron/cover.yaml
climate: !include crestron/climate.yaml
```

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
- `tests/test_xlsx_to_yaml.py`：`tools/xlsx_to_yaml.py` 的行→实体映射（灯光拆分 light/switch、空调字段、窗帘 type、重名去重）。
- `tests/loader.py`：把上述模块挂到合成包下单独加载，绕开会 `import homeassistant` 的真实 `__init__.py`。

> 实体级测试（light / switch / cover / climate / media_player）需要 `pytest-homeassistant-custom-component`，本仓库未集成。
