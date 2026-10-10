# Home Assistant Crestron XSIG 集成

通过 Crestron XSIG，把灯光、空调、窗帘、插座等设备接入 Home Assistant（HA），也可以让 Crestron 面板触发 HA 动作、接收 HA 状态。

当前集成版本为 `0.4.0`，支持 `light`、`climate`、`cover`、`switch`、`number`、`select`、`sensor`、`binary_sensor`、`media_player`。实体和连接参数使用 YAML 配置；UI 负责显示集成、设备、诊断和重新同步入口。

本文介绍使用方法；开发细节见 [ARCH.md](ARCH.md)，未完成事项见 [TODO.md](TODO.md)。

## 1. 安装与连接

### Home Assistant 侧

1. 把仓库的 `custom_components/crestron/` 复制到 **HA 配置目录**下的 `custom_components/crestron/`。
2. 在 HA 配置目录创建 `crestron.yaml`，最小内容如下：

   ```yaml
   port: 10200
   light:
     - name: 客厅灯带
       brightness_join: 1
   ```

3. 在现有 `configuration.yaml` 加入：

   ```yaml
   crestron: !include crestron.yaml
   ```

4. 检查配置并重启 Home Assistant。集成会根据 YAML 自动创建一个配置项；在“设置 → 设备与服务”查看 Crestron XSIG。

`!include` 的路径相对于包含它的文件。被包含的 `crestron.yaml` 从 `port:` 和平台列表开始，不再套一层 `crestron:`。实体列表内不需要 `platform: crestron`。

如果 YAML 放在组件目录，使用：

```yaml
crestron: !include custom_components/crestron/crestron.yaml
```

仓库自带的 `configuration.yaml` 只是这种路径安排的示例，请把需要的配置合并到自己的文件中。

### Crestron 侧

HA 是 TCP 服务端，Crestron 主控主动连接 HA：

1. 在主控程序配置 TCP/IP Client，目标地址为 HA 主机，端口与 `port` 一致。
2. 连接 XSIG（Intersystem Communication）符号与 TCP Client 的发送、接收通道。
3. 将设备控制和状态反馈接到对应的数字、模拟、串行 join；启用连接及断线重连逻辑。
4. 确保该 TCP 端口能从主控访问。集成监听 `0.0.0.0`，只维护一个活动客户端。

join 编号必须以主控程序的实际 XSIG 映射为准。YAML 数字字段只填整数编号：数字 join 为 `1–4096`，模拟和串行 join 为 `1–1024`。`d1` 与 `a1` 属于不同类型，可以同时使用。

本项目的成对开关控制约定：HA 向“开/关”join 发瞬动命令，主控在相同编号的反馈方向返回互斥状态。开高、关低表示开；开低、关高表示关；同高或同低不能确认状态。请在主控程序中实现这项约定，它不是任意 XSIG 程序自动具备的设备反馈。

## 2. 从 Excel 生成设备配置

转换器只使用 Python 标准库，可独立运行于 Python 3.8 及以上环境；不需要安装 HA、openpyxl 或 pandas。

复制根目录的 `sample.xlsx` 作为模板，保留所需工作表。表名和表头须使用下列文字，第一行写表头，列顺序可以调整：

| 工作表 | 推荐表头 | 生成结果与填写要求 |
|---|---|---|
| 灯光 | 序号、楼层、房间、名称、功能、亮度、色温、开、关 | 亮度＋色温生成调色温灯；仅亮度生成调光灯；仅完整开＋关生成开关灯 |
| 插座 | 序号、楼层、房间、名称、开、关 | 开、关必填，生成插座类型的 switch |
| 窗帘 | 序号、楼层、房间、名称、类型、开、关、停止、位置 | 开、关、停止必填，位置可选；生成 cover |
| 空调 | 序号、楼层、房间、开、关、制冷、制热、通风、除湿、低速、中速、高速、自动、温度、室温、风速值 | 开、关必填，其余控制和温度 join 可选；每行生成一个 climate |

填写要点：

- join 单元格填写十进制整数编号，例如 `414`，不是 `a414`、`414.0` 或当前温度。建议使用静态数字。
- “亮度、色温、位置、温度、室温”是模拟 join；开关、模式、风速是数字 join。
- 灯光不能同时填写模拟控制与开关控制；色温必须配合亮度。“功能”通常只作备注，不决定能力。
- 空行会忽略；“功能”为 `//` 且所有 join 都空的灯光模板行也会忽略。有名称但没有必需控制能力的普通设备行会报错。
- 空调“风速值”目前不转换，使用低速、中速、高速、自动四根数字 join。
- 窗帘类型支持 `awning`、`blind`、`curtain`、`damper`、`door`、`garage`、`gate`、`shade`、`shutter`、`window`；为空或无法识别时使用 `curtain`。
- “说明”工作表会忽略；其他未知工作表会警告。同一平台的重名实体会自动追加数字后缀。

在仓库根目录先检查：

```bash
python3 tools/xlsx_to_yaml.py --check sample.xlsx
```

检查通过后生成到一个待审阅文件：

```bash
python3 tools/xlsx_to_yaml.py sample.xlsx local/crestron.generated.yaml
```

第二个参数也可以是目录，此时输出文件名固定为 `crestron.yaml`。

**存在任意校验错误时，整次生成失败，不创建或覆盖输出 YAML。** 错误位置使用真实 Excel 行号，例如 `灯光!10`；工作表中间的空行不会让行号提前。警告不阻止生成，但应检查重复 join、表头拼写和被忽略字段。

成功生成会覆盖指定文件的全部内容，并写入默认 `port: 10200`。部署前检查实体与 join，按现场需要修改端口，并合并手写的 `to_joins`、`from_joins` 或其他平台配置；重新生成不会保留这些手工内容。备份旧文件后再替换 HA 实际引用的 YAML。

生成器还会填入 `device_id`、`device_name` 和区域建议。灯光、插座、窗帘名称来自“楼层.房间 名称”，空调来自“楼层.房间 空调”。

## 3. 手写或补充实体

以下示例均为 **被 include 的文件内容**，每个平台列表在同一文件中只定义一次。join 仅为示例，请换成现场编号。

### 灯光、开关、窗帘和空调

```yaml
port: 10200

light:
  - name: 客厅灯带
    brightness_join: 1
    color_temp_join: 201
    device_id: living_strip
    device_name: 客厅灯带
    suggested_area: 客厅
  - name: 走廊壁灯
    on_join: 11
    off_join: 12

switch:
  - name: 客厅插座
    on_join: 21
    off_join: 22
    device_class: outlet

cover:
  - name: 客厅窗帘
    type: curtain
    open_join: 31
    close_join: 32
    stop_join: 33
    pos_join: 301

climate:
  - name: 客厅空调
    on_join: 41
    off_join: 42
    set_temp_join: 401
    reg_temp_join: 402
    mode_cool_join: 43
    mode_heat_join: 44
    mode_fan_join: 45
    mode_dry_join: 46
    fan_low_join: 47
    fan_med_join: 48
    fan_high_join: 49
    fan_auto_join: 50
```

行为说明：

- **调光灯**：亮度在主控侧为 `0–65535`，HA 侧为 `0–255`；色温直接使用 `2700–6500 K`。调光灯显示依赖主控反馈，命令不会立即改写反馈缓存。
- **开关灯和 switch**：支持 `on_join`＋`off_join` 瞬动控制；单根保持电平控制可改用 `switch_join`。可选 `state_join` 提供独立反馈。switch 不允许同时配置这两种控制方式；可选 `mode_joins` 映射用于多状态反馈。
- **窗帘**：`pos_join` 直接使用 `0–100`，0 为全关，100 为全开。Excel 必须填写开、关、停止；手写 YAML 还支持仅 `pos_join`＋`stop_join`。没有位置 join 就没有 HA 百分比控制功能。尚未收到真实位置时使用推定位置；收到后，百分比显示优先使用真实反馈。
- **空调**：模式、风速、设定温度、室温按配置启用。没有模式 join 时提供关机和自动（纯开机）模式。温度直接使用摄氏整数，不做乘十或华氏换算。
- **空调设温**：允许 `16–30°C` 的整数值，`25.0` 等整数表示也可接受；小数部分非零、布尔值、非有限值及越界值会被拒绝，显示和发送值保持一致。没有设温 join 时不提供设温能力。
- **空调反馈**：未上报温度时保留未知或恢复值；真实零值会显示。设温写入范围不用于裁剪实时反馈，因此主控报告设定值 0 时会显示 0。室温与上一次接受值相差不足 `0.5°C` 时忽略。温度、模式、风速各自只根据对应通道的新反馈更新；其他状态变化或重连通知不会用旧缓存覆盖刚下发的设置。主控后来真实回传不同值时，HA 仍会更新显示。

同一台空调的开关、模式、风速、设温操作按收到的调用顺序执行；例如切换模式时需要先开机，会完成开机脉冲和模式选择后，再处理后续操作。不同空调仍可并行，不会自动在批量关机时加入间隔。

开关命令保留 2 秒反馈保护期；期满时会检查命令之后收到的开关反馈。没有新反馈时保留预显示值，不能据此确认设备已经执行。模式或风速反馈同时出现多个有效选项时，保留最近选择，等待明确反馈。

所有平台都可使用 `device_id`、`device_name`、`suggested_area`。只有填写 `device_id` 才创建设备关联；相同 ID 归入同一设备。区域是建议，不覆盖已有手工分区。实体改名不会改变其唯一标识；更换控制 join 或选项 join 组可能创建新实体。

### 其他平台

将需要的列表追加到同一份 `crestron.yaml`：

```yaml
number:
  - name: 通用整数设定
    value_join: 501
    min: 0
    max: 100
    step: 1

select:
  - name: 通风档位
    options:
      低速: 61
      高速: 62

sensor:
  - name: 温度读数
    value_join: 502
    divisor: 10
    unit_of_measurement: °C
    device_class: temperature
    state_class: measurement
  - name: 运行模式
    mode_joins:
      制冷: 63
      制热: 64

binary_sensor:
  - name: 门状态
    is_on_join: 65
    device_class: door

media_player:
  - name: 客厅音响
    mute_join: 66
    volume_join: 503
    source_number_join: 504
    sources:
      1: 电视
      2: 音乐
```

- `number` 的 `min/max/step` 及命令值必须为整数，默认 `16/30/1`，要求 `min < max`、`step > 0`。写入和历史恢复值受范围限制，实时反馈原样显示。现场取值请保持在 XSIG 无符号范围 `0–65535` 内。
- `select` 至少配置一个选项，选择时先清除其他 join，再置目标 join 为高；反馈全部低时保留最近选择。
- `sensor` 在 `value_join` 与非空 `mode_joins` 之间二选一。数值结果为原值除以正有限数 `divisor`，默认为 1；模式组全部上报且全低时显示“关闭”。
- `binary_sensor` 读取一根数字 join，未上报时为未知。
- `media_player` 必须填写静音、音量、音源 join 和非空音源表。音量 `0–1` 对应 `0–65535`；音源值 0 表示关闭，配置的音源编号应在 `1–65535` 内且名称不重复。开机使用最近一次有效音源反馈，否则使用配置中的第一个音源。

### 在网页上直接开关、调温和调风速

侧边栏“空调”的示例配置保存在 [dashboard/dashboard-climate.json](dashboard/dashboard-climate.json)，示例实体和位置须在部署前替换。每张卡片占 6 列、3 行；点击电源图标开关，下方调温和风速，点击名称打开详情。

其他安装可在仪表盘添加原生 Tile 卡片，用以下 YAML 替换实体 ID：

```yaml
type: tile
entity: climate.example_air_conditioner
icon: mdi:power
state_content:
  - state
  - current_temperature
tap_action:
  action: more-info
icon_tap_action:
  action: perform-action
  perform_action: climate.toggle
  target:
    entity_id: climate.example_air_conditioner
features_position: bottom
grid_options:
  columns: 6
  rows: 3
features:
  - type: target-temperature
  - type: climate-fan-modes
    style: icons
    fan_modes:
      - low
      - medium
      - high
      - auto
```

仅为配置了设温和相应风速 join 的实体添加这些控件。电源按钮通过 `climate.toggle` 调用集成的独立开关接口，不发送模式选择命令；实际启动模式以主控保留的模式为准。页面配置不需要重启 HA，也不需要安装前端插件。更多设置见 [Tile 卡片](https://www.home-assistant.io/dashboards/tile/)和[卡片功能](https://www.home-assistant.io/dashboards/features/)。

## 4. 面板控制与状态同步

### 主控触发 HA 动作：from_joins

在 `crestron.yaml` 顶层加入：

```yaml
from_joins:
  - join: d801
    script:
      - action: light.toggle
        target:
          entity_id: light.living_room
```

替换目标实体 ID。数字 join 只在本次连接中观察到 `0 → 1` 时执行：首次收到高电平不执行，重复高电平不执行；`0,1,0,1` 会执行两次。这是上升沿检测，没有时间防抖。断线或新连接接管会清空判断基线；主控应先上报低电平，否则连接后的第一次按键高电平可能不会触发。

模拟和串行 join 每次收到帧都会执行对应脚本。脚本可用 `{{ value }}` 获取本次值；模拟值以字符串传入，计算前使用 `int` 或 `float` 转换。

### HA 状态送回主控：to_joins

```yaml
to_joins:
  - join: d802
    entity_id: light.living_room
  - join: a801
    value_template: >-
      {% set volume = state_attr('media_player.living_room', 'volume_level') %}
      {{ (volume * 65535) | round(0) | int if volume is number else 'unknown' }}
  - join: s801
    entity_id: media_player.living_room
    attribute: media_title
```

可使用 `entity_id` 的状态、指定 `attribute`，或者 `value_template`；显式模板优先。这里的通用模拟映射不会自动缩放，直接发送音量 `0.5` 会得到整数 0，因此示例显式乘以 65535。

未知、不可用、空值不会作为有效状态发送。串行发送上限为 **252 个 UTF-8 字节**，超出后整条丢弃并记 INFO 日志。不要把同一写入 join 同时交给多个实体或重复的 `to_joins`；冲突会告警，但不会自动协调控制权。

## 5. 修改、重载与更新

| 操作 | 适用情况 | 作用 |
|---|---|---|
| 开发者工具 → 动作 → `crestron.reload` | 修改或重新生成 YAML 后 | 重新读取 YAML，重建集成与 TCP 监听 |
| 集成“选项” → `resync_to_joins` | 需要把 HA 状态再次发给面板 | 重新渲染并发送所有 `to_joins`，不读取 YAML |
| 重启 Home Assistant | 首次安装或更新 Python 代码后 | 重新加载组件代码和配置 |

`crestron.reload` 需要管理员权限，也可以在动作编辑器执行：

```yaml
action: crestron.reload
```

重载会短暂断开连接，主控需要重新连接。未知顶层键、错误的平台段结构会使重载被拒绝；个别实体校验或创建失败则会记录错误并跳过该实体，不保证整份设备列表原子加载。重载失败时会尝试恢复旧配置；恢复本身也可能失败，应查看日志并修正配置或重启。

删除 YAML 中的实体不会自动清理 HA 实体注册表。确认实体确已从本集成配置移除后，再在 HA 中清理相应遗留条目。

### 部署组件和仪表盘

`tools/deploy_to_ha.sh` 适用于能通过 SSH 访问 `/homeassistant` 且提供 `ha` CLI 的 HA 环境。运行脚本的电脑需要 Bash、curl、Python 3、ssh、scp，并能访问 GitHub 和 HA 网页；密码登录另需 sshpass。

安装和更新都会从 GitHub 下载**指定分支的最新完整源码**，组件、转换器和页面部署工具使用这份源码。`--branch 分支名` 默认是 **master**，与本地当前 Git 分支无关；下载不会覆盖本地工作区。需要 dev 分支上的更改时，必须加 `--branch dev`。

```bash
# 安装 master 最新版（默认模式，也可明确写 --install）
bash tools/deploy_to_ha.sh --host HA_HOST --user root --key

# 更新已有集成，使用 dev 最新版
bash tools/deploy_to_ha.sh --update --branch dev --host HA_HOST --user root --key
```

不带参数时会询问服务器地址、用户名及密码。安装模式还会询问是否生成、上传设备 YAML，默认否；`--update` 默认跳过这两步，保留现场设备配置，并检查目标已安装集成。需要更新设备配置时显式加 `--generate-yaml --upload-yaml`，也可用 `--xlsx FILE --yaml FILE` 指定本地输入和输出。默认路径仍是本地 `local/`，不会使用下载源码里的设备示例覆盖现场配置。

完整安装/更新会保存全部源码到服务器 `/homeassistant/crestron-source/<提交 SHA>/`，另将组件部署到 `/homeassistant/custom_components/crestron/`，并部署源码 `dashboard/` 中的页面。覆盖组件前会备份到服务器 `/homeassistant/crestron-backups/deploy.<随机后缀>/crestron/`。源码中的示例 `configuration.yaml` 只存档，不覆盖 HA 配置；首次安装仍需按第 1 节配置 include。显式上传的设备 YAML 固定放到组件目录内的 `crestron.yaml`，include 应指向该位置。

部署后先运行 `ha core check`，通过后保存页面，最后执行 **`ha core restart` 重启 Home Assistant 服务**使代码和配置生效，无需重启整台主机。脚本默认等待 HTTP API 恢复最多 120 秒，可用 `--restart-timeout 240` 调整；这仅确认 API 可访问，实体可用性及主控连接仍需在 HA 中检查。任何一步失败会停止后续步骤；已上传或保存的内容不会自动回滚。服务器上的源码和备份目录不会自动清理。

只更新组件、跳过页面及设备 YAML：

```bash
bash tools/deploy_to_ha.sh --update --branch dev --host HA地址 --user root --key --skip-dashboard
```

更多参数见 `bash tools/deploy_to_ha.sh --help`。

仅更新仪表盘，不上传组件或设备 YAML，不重启 HA：

```bash
bash tools/deploy_to_ha.sh --branch dev --host HA_HOST --user root --key --dashboard-only
```

仅页面模式同样下载指定分支，但不向服务器存档源码。若所选分支没有 `dashboard/`，完整安装/更新提示跳过页面，仅页面模式则报错。

部署页面需要**运行脚本的电脑安装 Node.js 22+**，无需 npm 依赖，HA 服务器不需要安装 Node.js。本机须能访问 HA 的 HTTP(S) 地址；默认从 `ha core info` 获取端口和 SSL 设置，也可用 `--ha-url https://HA地址` 或 `HA_URL` 环境变量指定。

认证优先使用 `HA_TOKEN` 环境变量中的管理员令牌；未设置时，通过 SSH 只读获取 `/homeassistant/.storage/auth` 中已有的有效所有者浏览器会话，换取临时访问令牌。这需要相应读取权限及远端 `jq`；没有可用会话时请提供 `HA_TOKEN`。令牌不会写入页面文件、备份或日志。

#### dashboard 文件与覆盖规则

`dashboard/` 纳入 Git；每个 `.json` 文件代表**一个完整仪表盘及其中的卡片**。部署会递归扫描目录，按文件路径排序。可以复制现有文件后修改：

- `dashboard`：新建页面的 `url_path`、`title`、可选 `icon`、`show_in_sidebar`、`require_admin`。
- `config`：HA 原始配置编辑器中的整个配置对象，包含非空 `views` 列表。

`url_path` 必须是含连字符的小写地址，例如 `dashboard-climate`；它是判断已有页面的唯一依据，标题相同不会被当成同一个页面。单张卡片不能单独作为部署文件，须放在 `config.views` 下的卡片列表中。复制到其他安装前请替换实际实体 ID；脚本不自动创建实体。

已有页面会逐项询问：

| 输入 | 行为 |
|---|---|
| `y` | 覆盖当前页面的整份 `config` |
| `n` 或直接回车 | 跳过当前页面 |
| `All yes` | 覆盖当前及本次部署后续所有已有页面 |
| `All no` | 跳过当前及本次部署后续所有已有页面 |

输入不区分大小写；`All` 的选择只在本次运行中生效。输入结束时，跳过所有剩余已有页面；不存在的页面仍会创建。已有页面的侧边栏标题、图标和权限等元信息保留，覆盖范围是页面内部布局与卡片。内置页面（包括 `/climate`）和 YAML 模式页面不能被此流程覆盖。

写入前在本地 `local/dashboard-backups/<时间与随机后缀>/` 保存原页面元信息和配置，备份失败则不写入；新页面的备份记录原先不存在。写入后回读核对。页面在确认期间被修改会中止部署；保存失败或连接中断时请先核对远端状态，脚本不会自动重试写入或回滚。恢复已有页面时，可将备份中的 `config` 粘贴到 HA 的原始配置编辑器；备份文件不要放入 `dashboard/` 参与部署。

可用 `--dashboard-dir DIR` 改用本地页面目录，部署工具仍来自指定 GitHub 分支；`--skip-dashboard` 完全跳过页面。修改本地页面后，可先使用下面命令检查，再通过 `--dashboard-dir dashboard` 部署预览；正常安装/更新使用 GitHub 中已发布的页面。只检查文件、不连接服务器：

```bash
node tools/deploy_dashboards.mjs --dir dashboard --check
```

## 6. 排查问题

### 已连接，但状态不对

连接成功不代表所有 join 已同步。集成在连接时发送 `0xFD` 请求同步，但没有“完整快照已结束”的判断；零值、串行值和设备反馈是否补齐取决于主控程序。集成不轮询，发送命令也不等于收到反馈。

断线后实体不可用；同一运行实例重连会保留上一次缓存。部分平台另有历史恢复或命令后的预显示，因此状态必须结合实际反馈判断。调光灯尚未收到亮度时目前会显示关闭，不能据此证明现场已关灯。

在集成菜单下载诊断，检查：

- `xsig.connected` / `xsig.available`：连接状态。
- `xsig.digital` / `xsig.analog`：收到的值，键为 join 编号字符串，例如 `"401"`；真实 0 与缺少此键含义不同。
- `xsig.serial`：只保留编号及内容长度，文本已隐藏。
- `xsig.cache_counts`：各类已缓存 join 数量。
- `configured_entities`：配置中的各平台条目数，不等于成功加载数。
- `join_usage.conflicts`：检测到的配置冲突。

某个 join 没有缓存，只能说明当前 hub 实例未成功接收并解析到它；应同时检查连接、编号、主控回传逻辑和解析日志。缓存存在也不保证是本次重连后更新的值。

### 临时开启日志

在 `configuration.yaml` 配置或合并：

```yaml
logger:
  default: warning
  logs:
    custom_components.crestron: info
    custom_components.crestron.crestron.frames: debug
```

INFO 可查看同步统计及串行超长丢弃；帧 DEBUG 日志可对应 join 与实体字段排查收发。统计中的“接收告一段落”不表示全量同步完整。如需查看空调命令与反馈值的对应关系，可再将 `custom_components.crestron.climate` 设为 `debug`；日志带设备名称，记录操作以及设温、模式、风速和开关反馈的变化。排查完成后按需要降低日志量。

测试环境和未验证边界见 [ARCH.md](ARCH.md#验证方法)，剩余改进见 [TODO.md](TODO.md)。
