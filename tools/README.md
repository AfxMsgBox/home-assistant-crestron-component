# Excel join 表制作与 YAML 生成指南

工作簿是 HA 配置人员与快思聪编程人员共同维护的设备接口表。`xlsx_to_yaml.py` 根据 join
列生成 `crestron.yaml`；设备导入后，`xlsx_to_automations.py` 根据关联列和 HA 注册表生成
`automations_manual.yaml`。只读取标准 `.xlsx`，不支持旧 `.xls` 或 CSV。

脚本只使用 Python 标准库，不需要安装 `openpyxl`、`pandas` 或 PyYAML。

## 1. 创建工作簿

最简单的方法是复制仓库根目录的 `sample.xlsx` 作为模板，再按项目修改。
也可以在 Excel、WPS 或 LibreOffice 中新建工作簿。

样表“说明”页包含双方职责、信号方向、数字/模拟 join 编号与运行值、关联列及联调步骤。
设备页蓝色表头是接口/设备字段，绿色表头是 HA 自动化关联字段；所有示例数据均为虚构，
使用前替换成实际接口及 HA 实体 ID，不需要的关联清空。示例关联覆盖单个、多个及共用控制源。

工作簿可以包含以下四个设备 sheet：

- `灯光`
- `插座`
- `窗帘`
- `空调`

sheet 名必须与上面完全一致。可以保留 `说明` sheet，转换器会将它作为说明页正常
忽略；其他未知 sheet 会产生警告。

每个设备 sheet 的第一行必须是表头：

- 不要在表头上方增加标题行。
- 不要合并表头单元格。
- 表头文字必须与本文一致，不能自行改名。
- 列的顺序可以调整，转换器按表头名称读取。
- 每个设备 sheet 都必须包含“楼层、房间、名称”，普通设备行这三项不能为空。
- 未使用的可选单元格保持空白即可。

### 自动化关联列

| 精确表头 | 填写内容 | 支持工作表 |
|---|---|---|
| HA关联开关实体ID | 可远程开关并反馈状态的 `switch.xxx`，品牌不限 | 灯光、插座、空调 |
| HA关联人在实体ID | 报告 on/off 的 `binary_sensor.xxx`，品牌不限 | 灯光、插座 |

关联字段不是快思聪 join，也不是 HA device_id。单元格内每行一个 ID，可用 Alt+Enter 换行；
不要用逗号分隔。留空/缺列表示不关联，相同 ID 可填在多条设备行中；空调不支持人在关联，
窗帘暂不支持关联。仅生成设备文件不会生成自动化，需在设备导入 HA 后执行第二步。
`python3 tools/xlsx_to_automations.py sample.xlsx --check-xlsx` 可离线检查样表格式，
实际实体是否存在仍要读取目标 HA 注册表确认。

## 2. join 填写规则

所有 join 单元格只填写十进制数字，例如 `701`：

- 数字 join：`1`–`4096`
- 模拟 join：`1`–`1024`
- 不要填写 `d701`、`a480`、`1.0`、`+1` 等格式。
- 建议直接填写静态数字，不要使用公式。

“亮度、色温、位置、温度、室温”等列填写的是模拟 **join 编号**，不是亮度、
色温、位置或温度的当前值。

`序号` 仅供人查看，转换器不会读取它。

### 控制与反馈约定

- HA 只监听 CP4N 推送，不轮询快思聪。
- 数字量“开/关”Join 既用于发送控制，也由 CP4N回传稳定状态。
- 状态回传中通常只有“开”或“关”一根为高；两根同时高或同时低视为过渡/未同步，
  HA 保留最近状态。
- HA 发出的开、关、停止命令不受当前反馈状态限制。
- 模拟量 join 由 CP4N 同步；调光灯显示依赖反馈，空调设温等支持命令预显示。
  预显示不能确认设备已执行，真实反馈仍会更新显示。

## 3. 灯光 sheet

推荐表头：

| 序号 | 楼层 | 房间 | 名称 | 功能 | 亮度 | 色温 | 开 | 关 | HA关联开关实体ID | HA关联人在实体ID |
|---|---|---|---|---|---|---|---|---|---|---|

示例：

| 序号 | 楼层 | 房间 | 名称 | 功能 | 亮度 | 色温 | 开 | 关 |
|---|---|---|---|---|---|---|---|---|
| 1 | 1F | 客厅 | 灯带 | 双色温 | 1 | 201 |  |  |
| 2 | 1F | 客厅 | 吊灯 | 单色温 | 2 |  |  |  |
| 3 | 1F | 走廊 | 壁灯 | Relay |  |  | 1 | 2 |

转换规则：

- “亮度”和“色温”都有 join：生成 `type: color_temp` 双色温 `light`。
- 只有“亮度”：生成 `type: brightness` 单色温 `light`。
- 只有完整的“开”和“关”：生成 `type: onoff` 只开关 `light`。
- 亮度运行值为 `0`–`65535`；色温运行值为 `2700`–`6500 K`。
- “亮度/色温”与“开/关”混合填写时，会因控制方式不明确而报错，阻止整次生成。
- 只有“色温”、开关不成对或没有任何能力的普通设备行会报错，阻止整次生成。
- 空行、以及“功能”为 `//` 且所有 join 为空的模板行会忽略。
- “功能”列只是备注，不参与类型判断。

## 4. 插座 sheet

推荐表头：

| 序号 | 楼层 | 房间 | 名称 | 开 | 关 | HA关联开关实体ID | HA关联人在实体ID |
|---|---|---|---|---|---|---|---|

示例：

| 序号 | 楼层 | 房间 | 名称 | 开 | 关 |
|---|---|---|---|---|---|
| 1 | 1F | 客厅 | 落地灯插座 | 147 | 148 |

“开”和“关”都是必填的数字 Join，也承载 CP4N回传状态。缺少任意一个时会
报错，阻止整次生成。生成 `switch`，并设置 `device_class: outlet`。

## 5. 窗帘 sheet

推荐表头：

| 序号 | 楼层 | 房间 | 名称 | 类型 | 开 | 关 | 停止 | 位置 |
|---|---|---|---|---|---|---|---|---|

示例：

| 序号 | 楼层 | 房间 | 名称 | 类型 | 开 | 关 | 停止 | 位置 |
|---|---|---|---|---|---|---|---|---|
| 1 | 2F | 示例房间甲 | 卷1 | shade | 700 | 701 | 702 | 480 |
| 2 | 2F | 示例房间乙 | 布帘2 | curtain | 704 | 705 | 706 | 481 |
| 3 | 1F | 露台 | 遮阳篷 | awning | 708 | 709 | 710 | 482 |

“开”“关”“停止”都是必填的数字 join；缺少任意一个时会报错，阻止整次生成。“位置”
是可选的模拟 join，运行时值约定为 `0`–`100`：

- `0`：全关
- `100`：全开

CP4N通过现有“开/关”Join 回传打开/关闭状态，不需要额外填写“正在打开”“正在
关闭”等反馈列。无论当前反馈是什么，HA 都允许再次发送开、关、停止命令。填写
“位置”后，HA 提供百分比控制；尚未收到位置反馈时使用乐观值并标记为假定状态。

“类型”支持以下 Home Assistant cover 类型：

| 类型 | 含义 |
|---|---|
| `awning` | 遮阳篷 |
| `blind` | 百叶帘 |
| `curtain` | 布帘、窗帘 |
| `damper` | 风阀、挡板 |
| `door` | 门 |
| `garage` | 车库门 |
| `gate` | 院门、大门 |
| `shade` | 卷帘、遮光帘 |
| `shutter` | 百叶窗、护窗 |
| `window` | 可开关窗户 |

类型忽略英文字母大小写和首尾空格。类型为空或不在支持列表中时，统一生成
`type: curtain`。转换器不会根据“卷1”“布帘2”等名称猜测类型。

## 6. 空调 sheet

推荐表头：

| 序号 | 楼层 | 房间 | 名称 | 开 | 关 | 制冷 | 制热 | 通风 | 除湿 | 低速 | 中速 | 高速 | 自动 | 温度 | 室温 | 风速值 | HA关联开关实体ID |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

示例：

| 序号 | 楼层 | 房间 | 名称 | 开 | 关 | 制冷 | 制热 | 通风 | 除湿 | 低速 | 中速 | 高速 | 自动 | 温度 | 室温 | 风速值 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 2F | 主卧 | 空调 | 400 | 401 | 402 | 403 | 405 | 406 | 407 | 408 | 409 | 410 | 400 | 401 | 450 |

转换规则：

- “开”和“关”是必填的数字 join；缺少任意一个时会报错，阻止整次生成。
- “制冷、制热、通风、除湿”是可选的数字 join。
- “低速、中速、高速、自动”是可选的数字 join。
- “温度”和“室温”是可选的模拟 join。
- 温度设定范围为 `16`–`30 °C`。
- “风速值”目前有意忽略：约定值为 `-1/10/50/100`，分别表示自动/低/中/高，
  现阶段使用四根数字量风速 Join。
- 数字量模式和风速 Join 由 HA 控制并由 CP4N回传。
- “风速值未使用”是信息提示，不阻止生成；如果低速/中速/高速/自动列均为空，生成配置不提供风速调节。
- 每行生成一个 `climate` 实体，名称来自“名称”列。旧表需要补上这列，例如填写“空调”。

## 7. 名称与重复项

四类设备的默认显示名均来自“名称”列，不附加楼层和房间前缀。

- 同一楼层、同一房间内重名的设备，全部从 1 编号，例如 `吊灯1`、`吊灯2`；
  跨设备类型也一起检查，不同房间可以使用相同名称。
- 原始唯一名称优先保留；若表里已有 `吊灯1`，其他重名“吊灯”的编号会跳过它。
- 相同“楼层＋房间”的设备行共用一个房间，不会因为重复行创建多个区域。
- 不同楼层有同名房间时，导入 HA 后从 1 编号，例如 `书房1`、`书房2`，
  并避开已有区域名。首次分配按位置排序，后续导入复用区域 ID 和已有编号。

生成的 YAML 包含 `device_id`、`device_name`、`floor`、`room`。新版集成加载时
创建或复用楼层与区域，把设备分配到相应区域；设备实体会继承设备的区域，原有实体
单独设置的区域会清除。HA 手工设置的自定义名称仍优先显示。

旧 YAML 只有 `suggested_area` 时保持原来的建议分区方式。要启用新版同步，需更新
组件、补齐 Excel 三个必填字段、重新生成并上传配置，再重载或重启 HA。原 join 和
`device_id` 不变时保留已有实体 ID；可复用并更名 `B2.会客厅` 等旧区域，保留区域 ID。

## 8. 运行转换器

在仓库根目录执行：

```bash
python3 tools/xlsx_to_yaml.py 你的设备表.xlsx 输出目录
```

脚本会生成：

```text
输出目录/crestron.yaml
```

也可以把第二个参数直接写成完整 YAML 文件路径：

```bash
python3 tools/xlsx_to_yaml.py \
  你的设备表.xlsx \
  custom_components/crestron/crestron.yaml
```

目标文件已经存在时会被覆盖，正式生成前请先备份手工修改过的 YAML。

成功输出示例：

```text
[信息] 已忽略说明工作表：说明
[信息] 灯光表：已忽略1个空行或模板行，行号：108 (模板行（功能为 //，所有信号编号为空）)
[信息] 灯光表：已转换205条设备配置
[警告] 灯光表第5行：同房间设备名“吊灯”重复，已改名为“吊灯1”
[警告] 灯光表第6行：同房间设备名“吊灯”重复，已改名为“吊灯2”
已生成 .../crestron.yaml（灯光：205，空调：18，窗帘：32，开关：9；错误：0，警告：2）
```

这些转换结果、最终数量汇总和“下一步”提示会原样复制到生成 YAML 的头部，并全部
加上 `#`，因此既能保留本次完整命令行输出，又不会参与配置解析。报告包含每个设备
sheet 的实体数；空行/模板行的 Excel 行号及忽略原因；未知 sheet、未知窗帘类型、
重复 Join、重复实体名及自动改名；`风速值` 等明确不采用的非空单元格；以及每一条
校验错误。缺少、未知、空白或重复表头也会逐项报告，避免列名拼错后整列能力静默
消失；缺少楼层/房间/名称或必填 join 表头、表头重复会作为错误。“灯光表第108行”指原始 Excel 第 108 行。

只检查、不生成文件：

```bash
python3 tools/xlsx_to_yaml.py --check 你的设备表.xlsx
```

检查会报告准确的 sheet、Excel 行号和原因。错误包括楼层/房间/名称为空、join 格式/范围错误、必填
join 缺失或灯光控制方式混填；未知窗帘类型、重复 Join、重复实体名会产生警告。
存在错误时，正常生成命令会输出完整错误清单后停止，且不会覆盖已有 YAML；警告
不会阻止生成。

## 9. 在 Home Assistant 中加载

将生成文件放到 Home Assistant 配置目录，并在 `configuration.yaml` 中加入：

```yaml
crestron: !include crestron.yaml
```

然后：

1. 打开生成的 `crestron.yaml`，确认顶部 `port:` 是实际 XSIG 监听端口。
2. 在 Home Assistant 中检查配置。
3. 首次安装或更新组件代码后重启 Home Assistant；仅修改设备 YAML，可在开发者工具
   → 动作执行 `crestron.reload`（需管理员权限）。

如果文件放在其他目录，`!include` 路径必须相对于 `configuration.yaml`。例如：

```yaml
crestron: !include custom_components/crestron/crestron.yaml
```

## 10. 常见问题

### 某个 sheet 被跳过

检查 sheet 名是否精确写成 `灯光`、`插座`、`窗帘` 或 `空调`。`说明`会正常忽略，
其他未知名称会产生警告。

### 某一行没有生成

检查楼层、房间、名称及必填 join 是否完整，并确认 join 单元格只含十进制数字。
普通设备行有错误时不会生成部分结果；只有空行和符合规则的灯光模板行会被忽略。

### 窗帘全部生成成 curtain

检查窗帘 sheet 的第一行是否真的包含精确表头“类型”，以及类型值是否在支持列表中。
没有“类型”列、单元格为空或值不受支持时，都会按 `curtain` 处理。

### 修改 Excel 后输出没有变化

确认保存的是传给命令的那一份 `.xlsx` 文件，并避免在 Excel 尚未保存时运行转换器。

### HA 报 join 越界

检查数字 join 是否在 `1`–`4096`，模拟 join 是否在 `1`–`1024`。转换器负责读取
表格，Home Assistant 集成会在加载 YAML 时执行最终范围校验。

## 11. 安装与更新到 HA 服务器

命令中的 `HA_HOST` 是占位符，请替换为实际服务器地址。空调仪表盘由通用模板在部署时读取目标 HA 的实体和位置生成，不含预设家庭布局；完整部署先重启并等待实体加载，再备份和保存页面。只安装非空调设备时可加 `--skip-dashboard`。

统一使用 `deploy_to_ha.sh`，支持 GitHub 分支或本地开发目录两种来源，通过 SSH/SCP
部署组件，通过 HA API 保存仪表盘。Shell 可单独下载，其余源码自动获取；缺少本地项目时自动使用 GitHub。有本地项目时，来源菜单第一项为本地（默认），第二项为 GitHub。选择 GitHub 后分页读取
全部分支供编号选择，有 master 时默认 master，否则第一项。

默认来源 `local`，无本地项目且未显式指定来源时用 GitHub；`--branch` 隐含 GitHub，默认分支 `master`。`--source local` 使用脚本
所在 tools/ 的上一级项目目录，包含未提交修改和新增源码，不能与 `--branch` 同用。
组件、转换器及页面工具均使用所选源码的临时副本；本地模式排除 .git、local/、虚拟
环境、缓存、.env 文件及 crestron.yaml，拒绝源码中的链接与特殊文件。

在仓库根目录运行：

```bash
# 交互选择来源，GitHub 来源会列出全部分支
bash tools/deploy_to_ha.sh

# 更新已有集成，使用 dev，默认保留设备 YAML
bash tools/deploy_to_ha.sh --update --branch dev --host HA地址 --user root --key

# 更新已有集成，使用当前本地源码
bash tools/deploy_to_ha.sh --update --source local --host HA地址 --user root --key

# 仅更新页面，无需重启 Core
bash tools/deploy_to_ha.sh --branch dev --host HA地址 --user root --key --dashboard-only
```

设备部署和自动化部署也可独立运行，来源、连接参数与 Shell 一致：

```bash
python3 tools/deploy_devices.py --source local --host HA地址 --user root --key --update
python3 tools/deploy_automations.py --source github --branch master --host HA地址 --user root --key \
  --xlsx local/HA设备接口配置表.xlsx
```

两个 Python 入口会复用 Shell 的来源准备流程；GitHub 下载完成后执行该提交内的工具。
单文件启动时，Excel 和成品默认使用 Shell 所在目录的 `local/`，需自行提供现场 Excel。

运行脚本的电脑需要 Bash、curl、Python 3、ssh、scp；部署页面另需 Node.js 22+，
密码登录另需 sshpass。目标服务器须提供 `/homeassistant`、`ha` CLI、`curl` 和 `tar`/`gzip`。

安装和更新均询问是否重新生成设备、自动化文件，默认否；安装另询问设备上传，更新默认不上传，可用
`--generate-yaml --upload-yaml` 显式开启。`--xlsx FILE` 和 `--yaml FILE` 可指定
本地输入及输出，默认使用本地 `local/`；上传的设备 YAML 固定放到远端组件目录，
HA 的 include 需指向该位置。源码中的设备示例和 Python 缓存不会随组件代码部署。

完整安装/更新仅上传运行所需文件，取消服务器源码存档。旧组件压缩为 `crestron.tar.gz`，
页面旧配置存为 `dashboard/*.json`，统一保存在服务器 `/homeassistant/crestron-backups/日期/时间-随机编号/`。
备份完成后
部署到 `/homeassistant/custom_components/crestron/`，运行 `ha core check` 和 `ha core restart`，通过 SSH 在服务器端检查 HTTP API，
随后等待实体加载、生成并保存页面；显示等待进度和失败原因，
默认等待 120 秒，可用 `--restart-timeout` 调整。`--ha-url` 指定的地址也须能从服务器访问。
已有页面提供 `y`、`n`、
`All yes`、`All no` 四种覆盖选择；`--skip-dashboard` 跳过页面。
失败会停止后续步骤，已写入内容不会自动回滚。本机下载、解压等临时文件退出时清理，
历史服务器源码存档和备份不会自动删除。所选分支的页面工具须支持 `--remote-backup-dir`，否则上传前停止；可更新该分支或用 `--skip-dashboard` 跳过页面。

完整参数：

```bash
bash tools/deploy_to_ha.sh --help
```

页面认证、覆盖和备份规则见根目录 [README：脚本安装与更新](../README.md#1-脚本安装与更新推荐)。


## 自动化关联：第二步单独生成

设备先通过 `xlsx_to_yaml.py` 导入 HA，再使用 `xlsx_to_automations.py` 生成自动化；两个工具都只需要 Python 3.8+ 标准库，支持 `.xlsx`，不支持旧 `.xls`。

| 列 | 填写方法 | 支持设备 |
|---|---|---|
| HA关联开关实体ID | 可远程开关的 `switch.xxx`，品牌不限，多个 ID 在单元格内换行 | 灯光、插座、空调 |
| HA关联人在实体ID | 报告 on/off 的 `binary_sensor.xxx`，多个 ID 在单元格内换行 | 灯光、插座 |

也兼容字面 `\n`，重复 ID 自动合并。列缺失或空白表示不关联。窗帘暂不支持；控制端开关不能同时被当作被控插座。自动化不修改空调模式、温度或风速。设备唯一标识只用于查找 HA 当前的 entity_id，不根据名称猜 ID。

推荐运行 `python3 tools/deploy_automations.py --source local --host HA_HOST --user root --key --xlsx local/HA设备接口配置表.xlsx`。它读取注册表、等待实体加载、打印关联清单、生成文件，再询问是否上传。关联清单同时保存在生成文件顶部，缺失位置显示“未分配”，便于以后核对。`--skip-upload-automations` 可只生成，`--upload-automations` 明确允许覆盖；默认输出 `local/automations_manual.yaml`，可用 `--automation-yaml` 修改。

如需离线使用，先只读导出注册表并生成本地文件（服务器地址按现场调整）：

```bash
python3 tools/deploy_automations.py \
  --xlsx local/HA设备接口配置表.xlsx --output local/automations_manual.yaml \
  --ha-url http://HA_HOST --upload no --export-registry local/ha_registry.json \
  -- ssh -o BatchMode=yes root@HA_HOST

python3 tools/xlsx_to_automations.py local/HA设备接口配置表.xlsx \
  local/automations_manual.yaml --registry local/ha_registry.json
```

注册表快照包括 `entities`、`devices`、`areas`、`floors`、`states`。实体 ID 或设备归属变化后需重新导出。加 `--check` 只检查和显示报告；加 `--check-xlsx` 仅检查表格与模板，不需要注册表。认证优先管理员 `HA_TOKEN`，否则使用服务器有效所有者浏览器会话；API 通过 SSH 在服务器访问，需要服务器 jq、curl。

源模板为根目录 `automations_manual_template.yaml`。手工使用时，删除三个占位标记，参照旁边注释填写触发器、开关映射、人在映射；三处都需保持一致。生成后的文件是单条自动化，可粘贴到 HA 单条 YAML 编辑器；脚本部署则使用独立 `automation manual` 引用，并保留其他自动化。固定 `id` 用于工具确认加载结果，请勿改动；`alias` 可改。更新自动生成文件会覆盖之前的手工修改，应优先修改 Excel 或模板。


## 清理已删除设备和实体记录

`clear_deleted_devices.py` 支持 `--target devices|entities|all`：分别清理设备注册表 `data.deleted_devices`、实体注册表 `data.deleted_entities` 或两者；默认 devices，兼容原命令。保留活跃记录及其他字段，不按集成筛选。在完整项目根目录运行：

```bash
python3 tools/clear_deleted_devices.py --host HA_HOST --target all --dry-run
python3 tools/clear_deleted_devices.py --host HA_HOST --target all
python3 tools/clear_deleted_devices.py --host HA_HOST --target entities
python3 tools/clear_deleted_devices.py
```

第一条仅预览所选范围的各自数量和 Core 状态。正式运行询问确认后停止 Core、复核删除集合、备份全部所选注册表，所有备份及新内容校验通过后才逐文件替换。失败时尝试安全回滚已改文件；原本运行则恢复启动，原本停止则保持停止，全部为空直接结束。服务器日期备份目录下分别为 `device-registry/core.device_registry` 和 `entity-registry/core.entity_registry`，临时文件正常退出时删除。

支持 `--password`、`--identity`、`--port`、`--color`；省略密码使用 SSH key，省略 identity 使用默认密钥/配置。不带参数逐步询问连接和范围，范围默认“设备”。需本机 Python 3、SSH（密码方式另需 sshpass）及同目录公共工具，服务器需 ha/jq/timeout/curl 和标准文件工具；不需要 HA API 令牌。`--yes` 跳过所选范围的确认，`--restart-timeout` 调整恢复 HTTP 的等待时间，`--ha-url` 覆盖自动识别地址。

不能确认停止、确认后删除集合变化或备份失败时不写入。两个文件不构成跨文件原子事务；回滚仅在所有所选文件均未被外部进程修改且 Core 仍停止时尝试，否则提示人工检查并不自动启动。异常断线/强制终止后先核查 Core、备份及 `.crestron-device-cleanup.lock`，不要直接重试。

清理不重命名现有实体；删除集成后清理对应遗留记录，才可避免重新添加时从删除记录恢复旧 ID。YAML 保留时，重启会尝试重新导入，名称由 HA 规则生成；之后需核对脚本/自动化/页面的引用。

## 查询服务器实体、设备和位置

`list_ha_entities.py` 只读列出匹配 entity_id 的实体，每行包含楼层、房间、设备名称、实体名称、完整 entity_id 和 HA device_id。关键词按“或”匹配，不区分大小写，不限制设备品牌。

```bash
python3 tools/list_ha_entities.py
python3 tools/list_ha_entities.py --host HA_HOST switch occ
python3 tools/list_ha_entities.py --host HA_HOST --password '你的密码' switch occ
python3 tools/list_ha_entities.py --host HA_HOST --identity ~/.ssh/id_ed25519 occ
python3 tools/list_ha_entities.py --host HA_HOST switch occ --format tsv > entities.tsv
```

以上命令在项目根目录运行；不带参数只询问一次连接信息，然后连续查询，每轮重新读取 HA，失败可重试，Ctrl+C 或输入流结束退出。密码输入隐藏。关键词留空默认 `_occupancy_status_p_` 和 `_oh4w_on_p_`，单独输入 `*` 查询全部；带参数时只执行一次，省略关键词同样使用默认值，查询全部使用带引号的 `'*'`。省略密码使用 SSH key；不指定 identity 时遵循 ~/.ssh/ 默认密钥、SSH 配置和 agent。脚本需同目录公共工具模块，密码登录另需 sshpass；服务器需 jq/curl/ha，API 认证复用已有所有者会话或 HA_TOKEN。

终端默认彩色、中文对齐，保留六列。实体名称里与设备名称重复的部分去掉，去除后为空显示 `—`；没有重复则原样显示，只改变查询输出。每条实体一行、ID 不截断。表格超宽或超高时自动进入本机 less 查看器，左右方向键横移、上下浏览，q 返回，Ctrl+C 退出；需要安装 less。重定向不分页且自动关闭颜色；--color always/never 可强制选择，--format tsv 始终输出无颜色完整数据。包含已注册停用实体和没有注册记录的实时实体；没有位置/设备时明确标注。
