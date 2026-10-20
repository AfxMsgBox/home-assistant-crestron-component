# Home Assistant Crestron XSIG 集成

把快思聪的灯光、空调、窗帘、插座等设备接入 Home Assistant（HA）。使用 YAML 配置，通过 Excel 生成设备列表；HA 监听 TCP，快思聪主控主动连接。

使用方法见下文，技术架构见 [ARCH.md](ARCH.md)，未完成事项见 [TODO.md](TODO.md)。

## 1. 脚本安装与更新（推荐）

目标 HA 服务器需支持 SSH、`ha`、`curl`、`tar`/`gzip` 命令，配置目录为 `/homeassistant`。

**从 GitHub 安装只需下载一个文件**，其余工具、组件和模板会自动下载：

```bash
curl -fL https://raw.githubusercontent.com/AfxMsgBox/home-assistant-crestron-component/master/tools/deploy_to_ha.sh -o deploy_to_ha.sh
bash deploy_to_ha.sh
```

没有本地项目时自动进入 GitHub 分支选择；有项目时显示 `1. 本地目录（默认）`、`2. GitHub`。GitHub 会列出全部分支，有 `master` 时默认选它，否则第一项。下载所选分支完整源码、检查所需文件后才开始部署。分支必须已包含新版部署工具；未发布的本地修改请使用本地来源。

以下命令中的 `HA_HOST` 请替换成你的 HA 地址。在本地项目根目录运行：

```bash
# 交互选择来源和 GitHub 分支
bash tools/deploy_to_ha.sh

# 安装：从 GitHub 下载 master 最新版
bash tools/deploy_to_ha.sh --source github --host HA_HOST --user root --key

# 更新：从指定分支下载最新版，默认保留现场设备 YAML
bash tools/deploy_to_ha.sh --update --source github --branch dev --host HA_HOST --user root --key

# 更新：使用本地开发代码，包含未提交修改和新增源码
bash tools/deploy_to_ha.sh --update --source local --host HA_HOST --user root --key
```

- 默认来源为 `local`；没有本地项目且未指定来源时自动使用 GitHub。`--source github` 默认分支为 `master`；单独指定 `--branch` 也表示选择 GitHub。带参数运行直接使用该分支，不显示菜单。
- `--source local` 从脚本所在 `tools/` 的上一级推断项目根目录，与启动命令时所在目录无关；不能与 `--branch` 同用。
- 本地模式先复制源码到临时目录，再从副本部署组件、转换器和仪表盘。排除 `.git`、`local/`、虚拟环境、缓存、`.env` 文件及 `crestron.yaml`；设备 YAML 仍须单独选择上传。源码中的符号链接或特殊文件会报错停止。
- 不带参数运行会询问连接信息；`--key` 使用 SSH 密钥，密码登录另需 `sshpass`。
- 部署终端统一显示彩色步骤、状态标签和对齐的配置信息；蓝色表示进行中、绿色表示完成、黄色表示提醒、红色表示错误。重定向到文件、设置 `NO_COLOR=1` 或使用 `TERM=dumb` 时输出纯文本。
- 关联清单按控制开关或人在传感器分组，列出双方角色、楼层、房间、名称和完整实体 ID。上传文件按列显示文件名、大小和最后修改时间（UTC），不显示传输速度；仪表盘显示本地模板文件信息。
- 运行脚本的电脑需要 Bash、curl、Python 3、ssh、scp；部署仪表盘另需 **Node.js 22+**，可用 `--skip-dashboard` 跳过页面。
- 部署中的所有 yes/no 询问回车默认 **Yes**，包括重新生成设备、重新生成自动化、上传配置和覆盖页面；输入 `n` 跳过，输入结束不视为确认。选择生成时需提供 Excel。更新模式仍默认保留服务器设备 YAML，可加 `--upload-yaml` 上传；可用 `--generate-yaml`、`--generate-automations` 明确生成，或用对应的 `--skip-*` 参数跳过。
- 生成会覆盖指定的本地 YAML，不保留手写内容。上传的 YAML 放在 `/homeassistant/custom_components/crestron/crestron.yaml`。首次安装需在 HA 的 `configuration.yaml` 加入：

  ```yaml
  crestron: !include custom_components/crestron/crestron.yaml
  ```

部署分两步：

1. **设备配置**：从 Excel 生成 `crestron.yaml`，按选择上传；备份组件、检查配置，重启 Home Assistant 服务并等待 HTTP 恢复，再生成并部署页面。
2. **自动化关联（可选）**：读取 Excel 和 HA 注册信息，等待涉及的实体注册并加载，按模板生成 `automations_manual.yaml`。列出双方的楼层、房间、名称和实体 ID，确认上传后备份旧文件、检查配置并重载自动化，无需再次重启。

入口脚本依次调用 `deploy_devices.py` 和 `deploy_automations.py`，也可分别运行，方便调试；两个 Python 入口使用相同的来源、连接参数，并复用 Shell 下载流程。第二步依赖设备已导入 HA。默认等待 120 秒，可用 `--restart-timeout 240` 调整。HTTP 和自动化接口均通过 SSH 在服务器执行，避免本机代理影响；实体存在不代表主控已在线。

```bash
# 完整两步：从本地源码部署，生成/上传设备，随后生成自动化并询问是否上传
bash tools/deploy_to_ha.sh --source local --host HA_HOST --user root --key \
  --xlsx local/HA设备接口配置表.xlsx --generate-yaml --upload-yaml --generate-automations

# 单独第一步：部署设备及仪表盘，检查并重启 HA
python3 tools/deploy_devices.py --host HA_HOST --user root --key \
  --xlsx local/HA设备接口配置表.xlsx --upload-yaml

# 单独第二步：设备已导入，只更新自动化关联，不重启 HA
python3 tools/deploy_automations.py --host HA_HOST --user root --key \
  --xlsx local/HA设备接口配置表.xlsx
```

Shell 入口加 `--devices-only` 或 `--automations-only` 也能单独执行某一步。自动化默认输出到设备 YAML 同目录，可用 `--automation-yaml FILE` 指定。生成报告也保存在成品顶部注释中。`--skip-upload-automations` 只生成本地文件；`--upload-automations` 表示已确认上传并覆盖服务器文件，省略该参数则生成后询问，回车默认上传并覆盖；输入结束时不上传。自动化阶段需服务器 `jq`，认证使用管理员 `HA_TOKEN` 或已有的有效所有者浏览器会话。

默认输入和输出使用项目的 `local/`；单文件启动时使用 Shell 所在目录的 `local/`，不会保存到临时下载目录。Excel 文件仍由你提供。

服务器只接收运行所需组件、选定的设备/自动化 YAML 和通过接口保存的仪表盘，不再保存整份源码、文档或测试。下载、解压和打包准备在本机临时目录进行，退出时自动清理。

**备份统一保存在 HA 服务器**，按服务器日期、时间分目录，同秒多次部署也不会覆盖：

```text
/homeassistant/crestron-backups/2026-10-10/153000-随机编号/
  crestron.tar.gz          旧组件及其中的设备 YAML（首次安装无此文件）
  dashboard/页面地址.json   页面修改前的配置
  automation/             第二步修改前的自动化文件和 configuration.yaml
```

组件压缩备份校验通过后才上传新组件；页面备份成功后才保存该页面。失败会停止后续步骤；第一步已写入的组件和页面不会自动回滚，第二步文件替换或配置检查失败会恢复该步原文件。历史源码存档和旧备份不会自动删除。

### 仪表盘

[dashboard/dashboard-climate.json](dashboard/dashboard-climate.json) 提供紧凑空调页面：每排两张卡片，电源图标直接开关，下方调温和风速，点击名称进入详情。文件只保存通用模板；每次部署时从目标 HA 读取实际空调实体、楼层和房间，按楼层分组生成，无须手填实体 ID。风速选项按设备支持的档位显示。

空调页面包含所有品牌已加载、未停用且未隐藏的 `climate` 实体；位置以实体设置优先，其次继承设备位置，缺失位置归入“未分配”。完整安装会先重启并等待实体加载，再生成页面；没有空调或加载超时会停止页面部署，不覆盖旧页面，仅安装其他设备时使用 `--skip-dashboard`。调整设备或位置后重新部署页面即可更新；页面不会自行新增卡片。

`dashboard/` 中每个 JSON 包含 `dashboard` 元信息与 `config` 页面配置。空调模板另含 `generator`，普通静态页面仍可直接填写完整 `config`。部署递归扫描所有 JSON，按 `url_path` 判断是否已存在；覆盖已有页面会替换整个页面配置，保留原页面元信息。

| 选择 | 含义 |
|---|---|
| 回车或 `y` | 覆盖当前页面（默认） |
| `n` | 跳过当前页面 |
| `All yes` | 覆盖本次所有已有页面 |
| `All no` | 跳过本次所有已有页面 |

覆盖询问默认 Yes，输入 `n` 可跳过；输入结束时按 All no 跳过剩余已有页面。

新页面仍会创建，并备份其原先不存在的记录。页面备份保存到服务器本次部署目录的 `dashboard/` 下；仅部署页面时也创建独立的日期备份目录。网页中的修改不会自动同步回仓库文件，确认覆盖后会被重新生成的配置替换。生成的实体 ID 和名称只用于 HA 页面，不写回仓库；服务器备份也包含现场配置，请保留在私有环境。

```bash
# 仅部署仪表盘，不重启 HA
bash tools/deploy_to_ha.sh --branch dev --host HA_HOST --user root --key --dashboard-only

# 查看完整参数
bash tools/deploy_to_ha.sh --help
```

本机需能访问 HA 网页。可用 `--ha-url https://HA地址` 指定访问地址；完整部署时服务器端也需能访问该地址，用于重启后的检查。页面认证优先使用 `HA_TOKEN` 环境变量中的管理员令牌；未设置时，通过 SSH 读取已有的有效所有者浏览器会话并换取临时令牌，需要远端 `jq`。没有可用会话时需提供令牌。

## 2. 手动安装与连接

1. 将 `custom_components/crestron/` 复制到 HA 配置目录下的同名目录。
2. 创建设备 YAML（见下一节），在 `configuration.yaml` 引入，例如：

   ```yaml
   crestron: !include crestron.yaml
   ```

3. 检查配置并重启 HA，在“设置 → 设备与服务”查看 Crestron XSIG。
4. 在快思聪程序中配置 TCP/IP Client，连接 **HA 地址与 YAML 中的端口**，并接好 XSIG 收发通道，启用断线重连。

`!include` 路径相对于包含它的文件。设备 YAML 从 `port:` 开始，不再包一层 `crestron:`，平台列表内不需要 `platform: crestron`。

join 必须与主控程序一致：数字 join 为 `1–4096`，模拟和串行 join 为 `1–1024`。成对开关使用瞬动控制，主控应在相同编号的反馈方向返回互斥的开/关状态；这需要主控程序配合。

## 3. 工作簿：设备接口与自动化关联

这份工作簿供 **HA 配置人员和快思聪编程人员共同使用**：双方核对设备位置、名称及 join 接口，HA 再据此生成设备配置和关联自动化。复制 [sample.xlsx](sample.xlsx)，先阅读其中的“说明”页，再按现场填写。示例 join 和实体 ID 均为虚构数据，使用前须替换或清空。

两个转换器只需 Python 3.8+，无需额外依赖；只支持 `.xlsx`，旧 `.xls` 请先另存为 `.xlsx`。

| 工作表 | 必填内容 | 可选接口 | 可选 HA 关联列 |
|---|---|---|---|
| 灯光 | 楼层、房间、名称；亮度或完整开＋关 | 色温（需配合亮度）；调光灯也可填写完整开＋关 | HA关联开关实体ID、HA关联人在实体ID |
| 插座 | 楼层、房间、名称、开、关 | — | HA关联开关实体ID、HA关联人在实体ID |
| 窗帘 | 楼层、房间、名称、开、关、停止 | 类型、位置 | 暂不支持 |
| 空调 | 楼层、房间、名称、开、关 | 模式、风速、温度、室温 | HA关联开关实体ID；暂不支持人在关联 |

第一行是表头，列顺序不限。join 列填整数编号，例如 `414`，不是 `a414` 或当前温度。空行忽略；普通设备行的楼层、房间、名称不能为空。旧空调表需要补上 **名称** 列，例如填写“空调”。详细填写规则见 [Excel 指南](tools/README.md)。

**关联列填写 HA 实体 ID，不是 join 编号或 device_id**；品牌不限。`HA关联开关实体ID` 填可远程控制并反馈状态的 `switch.xxx`，`HA关联人在实体ID` 填报告有人/无人的 `binary_sensor.xxx`。一个单元格内每行一个 ID（例如 Alt+Enter 换行），留空表示不关联；同一 ID 填在多条设备行中可控制多台设备。不要改表头，也不要用逗号拼接 ID。样表包含单个、多开关、多传感器、共用控制源及留空示例。

先核对接口、生成并导入 `crestron.yaml`；设备在 HA 中注册后，再读取同一工作簿和 HA 注册表，生成 `automations_manual.yaml`。设备转换器不会自动生成关联。只修改关联列时，只需重新执行部署第二步。

```bash
# 先检查，不写文件
python3 tools/xlsx_to_yaml.py --check 你的设备表.xlsx

# 同时检查自动化关联列的填写格式，不需要连接 HA
python3 tools/xlsx_to_automations.py 你的设备表.xlsx --check-xlsx

# 检查通过后生成
python3 tools/xlsx_to_yaml.py 你的设备表.xlsx local/crestron.yaml
```

`--check-xlsx` 只检查填写格式和模板；实体是否存在、启用及加载，需要第二步读取目标 HA 注册表后才能确认。

转换提示和生成 YAML 的注释使用中文，位置显示为“空调表第13行”。有错误时整次生成失败，原输出文件保持不变。成功生成会覆盖整个目标文件，默认端口为 `10200`；需自行检查端口，并合并手写的其他平台配置、`to_joins` 和 `from_joins`。

### 楼层、房间和名称

新版配置包含 `floor`、`room`，加载后自动创建或复用 HA 楼层和区域，并把设备放入对应区域：

- 设备和实体的默认显示名来自 **名称** 列，不附加楼层、房间前缀。
- 同一房间重名的设备从 1 编号，例如 `吊灯1`、`吊灯2`；跨设备类型也一起检查，不同房间可以同名。
- 相同“楼层＋房间”的多行设备共用一个房间。不同楼层出现同名房间时，按 `书房1`、`书房2` 编号。
- 已存在的带数字名称会被保留，自动编号避开冲突。重复导入复用区域 ID；可识别原来的 `1F.厨房` 这类区域并迁移。

**已有安装**：更新组件后，用新版转换器重新生成并上传 YAML，再重载或重启 HA。只更新代码而继续使用旧 YAML，不会启用楼层、房间同步。

保持原 join 和 `device_id` 时，已有实体 ID、设备关联及自动化引用保持不变。HA 中手工设置的名称仍优先，需清除自定义名称才能显示 Excel 名称。含 `floor`、`room` 的设备以 YAML 为区域来源；重载会重新分配设备区域，并清除相应实体单独设置的区域，使其继承设备区域。旧 YAML 仅有 `suggested_area` 时仍使用原来的区域建议方式。

### 关联开关与人在自动化

设备配置仍由 `tools/xlsx_to_yaml.py` 单独生成。设备导入后，用独立工具 `tools/xlsx_to_automations.py` 读取 Excel、HA 注册表和 [自动化模板](automations_manual_template.yaml) 生成关联。推荐使用上面的部署脚本自动完成注册信息读取。

- “HA关联开关实体ID”：填写控制开关的 `switch.xxx`，支持灯光、插座、空调；品牌不限，必须可通过 HA 远程开关并反馈状态。
- “HA关联人在实体ID”：填写报告 `on/off` 的 `binary_sensor.xxx`，支持灯光与插座。
- 多个 ID 在单元格内换行；也兼容字面 `\n`。一个开关填到多行表示控制多台设备；一个设备可关联多个开关或传感器。
- 三类设备都双向同步：任意设备开启算开，全部明确关闭才算关。组内已有设备开启时，再收到开关开启状态不会强制打开其余设备。空调只控制开关。
- 多人在传感器：任意有人开启，全部持续无人 30 秒关闭。没有手动保持，共用队列时其他操作可能等待最多 8 秒关机反馈。

关联报告中，被控设备的位置/名称来自 Excel；开关和传感器的位置/名称来自 HA，缺失位置显示“未分配”。匹配使用集成唯一标识，不根据名称猜测实体 ID；缺失、禁用或歧义会停止生成，保留旧成品。

已有注册表导出文件时，也可以离线生成：

```bash
python3 tools/xlsx_to_automations.py local/HA设备接口配置表.xlsx \
  local/automations_manual.yaml --registry local/ha_registry.json
# 加 --check 只检查并输出关联清单，不写文件
```

生成的文件是单条自动化，可在 HA 单条自动化 YAML 编辑器粘贴。部署工具上传到 `/homeassistant/automations_manual.yaml`，检查并按需添加 `automation manual: !include automations_manual.yaml`；保留其他自动化文件。配置检查失败会恢复第二步修改前的文件；重载失败会明确报告并保留备份。模板中各占位区域旁有完整手填示例，原模板不能直接导入 HA。尚需现场验证实际联动效果。

## 4. 日常使用与修改配置

**调光灯已知限制**：先置零、等待约 0.2 秒再恢复亮度，已验证单灯开灯有效，但并发批量开灯曾出现闪亮；直接重发同一亮度则曾无法开灯。当前代码按用户要求恢复先置零，等待缩短为 0.1 秒；目标服务器文件和收发日志已核对该时序，实物效果仍待确认。修改代码前须阅读 [调光灯修改前必读](ARCH.md#调光灯修改前必读)，避免重复引入已知问题。

- **带开关 Join 的调光灯**：Excel 同时填写“亮度”和完整的“开、关”（支持色温再填“色温”）。开灯通过亮度 Join 先置零，等待 0.1 秒，再恢复记忆亮度，不发送数字开灯命令。批量开灯时各灯分别恢复自己的记忆亮度。关灯优先发送数字关灯命令，不改亮度；开关显示仍按数字反馈判断，主控须同步稳定状态。
- **没有开关 Join 的调光灯**：开灯先发送 0，等待 0.1 秒，再恢复记忆亮度，兼容只响应电平变化的主控；关灯记住非零亮度，重发后等待约 0.2 秒再置零。亮度反馈大于 0 为开、0 为关。
- 两种调光灯都记住最近一次非零亮度，关灯的零反馈不会覆盖记忆，正常重启后也可恢复。外部面板调光需由主控反馈到 HA，才会更新记忆。没有有效记忆时开灯默认 100%；显式指定亮度优先，指定 0 走关灯逻辑。已开灯时调光直接发送，不经过置零。
- 开灯的 0.1 秒等待与关灯延迟、数字脉冲宽度独立；关灯原有时序保持不变。批量开灯仍按各灯独立执行，是否闪亮需现场观察。
- 新增开关 Join 后，更新组件并重新生成、上传设备 YAML，再重启 HA；原亮度 Join 不变时保留实体身份。手写 YAML 也支持 `brightness_join` 配合 `on_join`＋`off_join`，或配合保持电平的 `switch_join`；可用 `state_join` 指定独立数字反馈。数字反馈未收到时显示未知，成对反馈同时高或同时低时保留最后明确状态，不用亮度猜测。
- 空调可在独立空调仪表盘直接开关、调温和调风速，复杂设置进入实体详情。电源按钮调用独立开关，不发送模式选择命令；启动模式由主控保留。
- 设温支持 `16–30°C` 整数；模式、风速和温度控件取决于已配置的 join。显示以主控真实反馈为准。
- 修改 Excel 后需重新生成 YAML，上传到 HA 实际引用的路径；生成不会保留此前手改内容。

| 修改内容 | 生效方式 |
|---|---|
| 设备 YAML | 开发者工具 → 动作 → `crestron.reload`（管理员） |
| Python 组件代码 | 重启 Home Assistant |
| 仪表盘 JSON | 重新部署页面，刷新浏览器 |
| 仅重新发送 HA 状态到面板 | 集成选项 → `resync_to_joins` |

重载会短暂断开 TCP，主控需重新连接。删除 YAML 行不会自动删除旧实体、设备、区域或楼层，请确认不再使用后在 HA 清理。

手写配置还支持 `number`、`select`、`sensor`、`binary_sensor`、`media_player`。面板触发 HA 动作使用 `from_joins`，HA 状态送回主控使用 `to_joins`；字段及行为见 [ARCH.md](ARCH.md)。

### HA 内置灯光页面的区域总开关

`/light` 的区域按钮在全部灯关闭时调用 `light.turn_on`，任意灯开启时调用 `light.turn_off`；按钮本身不指定亮度。HA 将批量请求并发分发给灯实体。当前 Crestron 调光灯在关闭时各自置零、等待 0.1 秒、恢复自身记忆；普通开关灯发送各自开灯命令。灯之间没有额外启动间隔，批量耗时不是“灯数 × 0.1 秒”。缺失有效记忆的调光灯才默认全亮，实物是否闪亮仍需观察。

批量实物闪亮的原因尚未定位；日志只出现记忆亮度，不代表灯具中间没有闪亮。需核查主控输入、调光模块控制输出和实物动作，不能直接认定“并发开灯就会先到 100%”。

### 单灯开关现场测试

在灯具旁观察时运行（会实际控制指定灯，结束后保持关闭）：

```bash
python3 tools/test_ha_light.py --host HA_HOST --entity light.example_light
```

默认倒计时 5 秒后开灯，观察 5 秒，再关灯；输出每秒 HA 状态、亮度和记忆值。可用 `--countdown`、`--interval` 调整等待，支持 `--password`、`--identity`；省略密码使用 SSH key。脚本只调用服务器当前版本的 `light.turn_on/turn_off`，不附加亮度，不部署代码；开灯失败也尝试关灯。HA 反馈不能代替实物观察，未部署的代码不在本次验证范围内。

## 5. 查找 HA 实体 ID

在项目根目录运行，查询过程只读，不修改 HA：

```bash
# 连接信息只输入一次，随后连续查询，Ctrl+C 退出
python3 tools/list_ha_entities.py

# 带参数只查询一次；entity_id 包含 switch 或 occ 就显示
python3 tools/list_ha_entities.py --host HA_HOST switch occ

# 省略关键词使用默认筛选；单独传入带引号的 '*' 查询全部
python3 tools/list_ha_entities.py --host HA_HOST '*'

# 指定密码；没有密码时使用 SSH key
python3 tools/list_ha_entities.py --host HA_HOST --password '你的密码' switch occ

# 指定私钥；不指定则由 SSH 使用 ~/.ssh/ 的默认密钥、配置及 ssh-agent
python3 tools/list_ha_entities.py --host HA_HOST --identity ~/.ssh/id_ed25519 occ

# 导出无颜色的制表符表格，方便复制到 Excel
python3 tools/list_ha_entities.py --host HA_HOST switch occ --format tsv > entities.tsv
```

默认用户 `root`、端口 `22`。密码交互输入隐藏；命令行密码可能留在 shell 历史中。密码登录需本机 `sshpass`，服务器需 `jq`、`curl` 和 `ha`。脚本复用 `tools/` 内的公共查询/连接模块，需在完整项目中运行。

无参数时每次查询后可继续输入关键词，失败后也可重试，每轮重新读取 HA。回车使用 `_occupancy_status_p_` 和 `_oh4w_on_p_` 两个默认关键词，输入 `*` 查全部；带参数运行时省略关键词也使用默认值。`Ctrl+C` 退出，输入流结束时同样退出。

彩色表格保留六列：楼层、房间、设备名称、实体名称、完整 `entity_id` 和 **HA 的 `device_id`**。实体名称中重复的设备名称会去掉，例如“厨房开关 左键”显示为“左键”，去掉后为空显示 `—`；仅调整输出，不修改 HA。多个关键词按“或”匹配、不区分大小写；只匹配 ID，不匹配名称，不作正则表达式解释。包含停用的已注册实体及无注册记录的当前实体；位置缺失显示“未分配”。

每条实体一行，中文按显示宽度对齐，ID 不截断。表格超出终端宽度或高度时自动用本机 `less` 查看：左右方向键横向滚动、上下翻阅，按 `q` 返回继续输入关键词，`Ctrl+C` 退出脚本；带参数运行时按 `q` 结束。需要本机安装 `less`。重定向和 TSV 导出直接输出完整内容，不启动查看器。

颜色默认只在终端开启，重定向自动关闭；可用 `--color always|never` 控制，也支持 `NO_COLOR`。API 认证使用管理员 `HA_TOKEN`，或服务器已有的有效所有者会话；`--ha-url` 可指定 HA 网页地址。

## 6. 清理已删除设备和实体记录

`clear_deleted_devices.py` 清空所选注册表中的已删除记录，保留仍在使用的设备、实体和其他字段。`--target devices` 清理 `deleted_devices`（默认，兼容旧命令），`--target entities` 清理 `deleted_entities`，`--target all` 清理两者。在项目根目录运行，将 `HA_HOST` 替换为实际地址：

```bash
# 只读预览两个注册表，不停止或修改 HA
python3 tools/clear_deleted_devices.py --host HA_HOST --target all --dry-run

# 清理两者：显示各自数量，询问确认后执行
python3 tools/clear_deleted_devices.py --host HA_HOST --target all

# 仅清理已删除实体记录
python3 tools/clear_deleted_devices.py --host HA_HOST --target entities

# 不带参数时逐步询问连接信息和清理范围
python3 tools/clear_deleted_devices.py
```

支持 `--password`、`--identity`；不提供密码时使用 SSH 默认密钥和配置。目标服务器需提供 `ha`、`jq`、`timeout`、`curl`，配置目录固定为 `/homeassistant`。终端默认彩色，可用 `--color never` 关闭。

确认后停止 Core，完整备份所选注册表，再清理并恢复原运行状态。两个文件的备份和新内容全部校验通过后才开始写入。备份保存在服务器同一个日期目录中：

```text
/homeassistant/crestron-backups/日期/时间-随机编号/
  device-registry/core.device_registry
  entity-registry/core.entity_registry
```

只备份所选范围；所选记录总数为零则直接退出。Core 原本已停止则保持停止。确认默认为“否”，`--yes` 跳过确认，仅作用于所选范围。原本运行时，清理后等待 HTTP 恢复，默认 120 秒，可用 `--restart-timeout` 调整。

两个文件逐个替换；部分写入失败时会尝试恢复本次已修改的文件。若检测到其他程序改文件或启动 Core，则停止自动恢复和自动启动，提示人工核查。SSH 断线、强制终止或断电后，先检查 Core、备份和锁目录 `/homeassistant/.crestron-device-cleanup.lock`，不要直接重复清理。

**清理不会改变现有实体 ID。** 若要重新分配 ID，应先删除对应集成，再清理其遗留的已删除记录；该工具按设备/实体类别清理全部已删除记录，不按集成筛选。保留 `crestron:` YAML 时重启 HA 会尝试重新导入，新 ID 由 HA 命名规则决定，不保证自动包含新楼层或房间。重建后需要检查旧自动化和页面引用，重新生成本项目的关联自动化及动态仪表盘。

## 7. 常见问题

- **HA 已重启但脚本仍在等待**：新版通过服务器端检查接口，200/401 表示恢复。超时时查看打印的地址、HTTP 状态和 SSH/curl 错误；接口可访问仍不代表所有实体已就绪。
- **所选分支的页面工具不支持服务器备份**：更新分支中的 `tools/deploy_dashboards.mjs`；暂不部署页面时可加 `--skip-dashboard`。脚本会在上传前检查，避免使用旧工具把备份写到本机。

- **更新后没变化**：检查部署来源。GitHub 来源只使用目标分支已发布的代码；本地开发修改可通过 `--source local` 部署。设备变化还需更新现场 YAML。
- **设备没有进入楼层、房间**：确认 YAML 有 `floor`、`room`、`device_id` 和 `name`，并查看是否有 `floor/room synchronization failed` 日志；修正后重载。
- **已连接但状态不对**：核对 join 和主控反馈。HA 不轮询设备；界面预显示不代表命令已经执行，主控后来回传不同值时仍会更新。
- **动作失败或无法加载**：查看“设置 → 系统 → 日志”中的 `custom_components.crestron`。临时启用 debug 后，完成排查请关闭：

  ```yaml
  logger:
    logs:
      custom_components.crestron: debug
  ```
