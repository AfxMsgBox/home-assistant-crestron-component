# 工具使用

以下命令均在项目根目录运行，`HA_HOST` 替换为 HA 地址。安装总说明见 [README](../README.md)，工作簿填写见 [Excel 指南](../docs/EXCEL.md)。

## 部署

```bash
# 总入口：选择本地 / GitHub 来源，依次部署设备、页面和可选自动化
bash tools/deploy_to_ha.sh

# 分开执行，便于调试；无参数会提示选择来源和连接信息
python3 tools/deploy_devices.py
python3 tools/deploy_dashboards.py
python3 tools/deploy_automations.py

# 只更新页面，使用本地最新修改
python3 tools/deploy_dashboards.py --source local --host HA_HOST --user root --key

# 从 GitHub 指定分支部署页面；先下载，再运行该分支的工具和卡片
python3 tools/deploy_dashboards.py --source github --branch master --host HA_HOST --user root --key

# 更新组件，保留服务器设备 YAML
python3 tools/deploy_devices.py --update --source github --host HA_HOST --user root --key

# 只重新生成并部署自动化，设备需已在 HA 注册
python3 tools/deploy_automations.py --host HA_HOST --user root --key --xlsx local/设备表.xlsx
```

| 工具 | 用途 |
|---|---|
| `deploy_to_ha.sh` | 单文件启动器：选择来源、下载/准备源码、询问选项、调用下面三个子工具 |
| `deploy_devices.py` | 生成设备 YAML，备份并上传组件/选定 YAML，检查并重启 HA Core |
| `deploy_dashboards.py` | 单独部署 `dashboard/` 全部页面及选中页面的 JS 卡片资源 |
| `deploy_automations.py` | 读取 Excel 和 HA 注册表，展示关联、生成 YAML、上传并重载自动化 |
| `deploy_common.py` | 内部公共模块：SSH、备份、等待 HTTP、颜色与对齐 |
| `deploy_dashboards.mjs` | 内部 Node 后端：模板校验、页面生成、覆盖询问、资源注册及回读 |

三个 Python 入口均复用 Shell 的来源选择和下载流程，不要单独复制一个 Python 文件使用。**只有 `deploy_to_ha.sh` 可以单文件下载运行**；其他依赖会自动从 GitHub 下载。默认本地来源、GitHub 默认 `master`；无参数选择 GitHub 时列出分支。

连接使用 `--host`、`--user`、`--port`；`--key` 用默认 SSH 密钥，`--identity FILE` 指定密钥。密码可用 `--password`，建议使用 `HA_SSH_PASSWORD` 环境变量或交互输入，密码登录需 `sshpass`。完整参数见 `bash tools/deploy_to_ha.sh --help`。

- 页面部署需本机 **Node.js 22+**，及可访问的 HA 网页。可用 `--ha-url URL` 指定地址；认证使用 `HA_TOKEN` 管理员令牌或服务器已有所有者会话。
- 所有部署 yes/no 询问回车默认 Yes，EOF 不视为同意。页面覆盖可选 `y`、`n`、`All yes`、`All no`。更新模式默认保留设备 YAML。
- 页面通常无需重启。完整设备安装会在 Core 重启前准备 `www`；单独部署时如没有该目录，会询问是否初始化并重启 Core。已有 `www` 但 `/local` 尚未生效时会报错提示重启，不误报成功。
- 已开启设备页仅收录灯光、空调、插座（`device_class: outlet`）、风扇，自动纳入符合条件的新增设备，状态立即更新，位置变更通过事件刷新并每 30 秒补查。空调页仍在部署时生成；新增空调或改位置后重新部署空调页。
- 自定义卡片随脚本上传并注册，无需 HACS；HA 资源需使用存储模式。更新卡片后刷新浏览器。
- 服务器只留运行文件与日期备份：`/homeassistant/crestron-backups/日期/时间-编号/`。页面、旧卡片及资源注册信息在其 `dashboard/` 内；本机临时文件退出时清理。
- 彩色输出自动适配终端；`NO_COLOR=1` 可关闭颜色。文件显示大小和 UTC 修改时间，不显示速度。

## 转换、查询与维护

| 工具 | 用途 |
|---|---|
| `xlsx_to_yaml.py` | 将工作簿转成 `crestron.yaml`，不连接服务器 |
| `xlsx_to_automations.py` | 读取工作簿、注册表及模板生成关联；通常由部署工具调用 |
| `list_ha_entities.py` | 只读查询实体、设备名、完整 ID、楼层和房间 |
| `clear_deleted_devices.py` | 预览并清理已删除注册记录；修改前停止 Core，备份后恢复原运行状态 |
| `test_ha_light.py` | 现场单灯开关观察，会实际操作所选灯，结束目标为关灯 |

```bash
# 校验工作簿和关联列，不写文件、不连接 HA
python3 tools/xlsx_to_yaml.py --check local/设备表.xlsx
python3 tools/xlsx_to_automations.py local/设备表.xlsx --check-xlsx

# 生成设备文件
python3 tools/xlsx_to_yaml.py local/设备表.xlsx local/crestron.yaml

# 连续查询；默认关键词为 _occupancy_status_p_ 和 _oh4w_on_p_
python3 tools/list_ha_entities.py

# 一次查询，多个关键词按“或”匹配；'*' 表示全部
python3 tools/list_ha_entities.py --host HA_HOST switch occ

# 只预览设备和实体的已删除记录
python3 tools/clear_deleted_devices.py --host HA_HOST --target all --dry-run

# 查看单灯观察工具用法
python3 tools/test_ha_light.py --help
```

实体查询保留六列，实体名称去除重复设备名，ID 不截断；窄屏使用 `less` 左右滚动，`q` 返回查询，`Ctrl+C` 退出。`--format tsv` 输出便于脚本处理的完整数据。

清理工具的 `--target devices|entities|all` 默认 `devices`；仅处理已删除记录，不改活跃实体 ID。执行清理前先预览并阅读提示，需更详细参数时使用对应工具的 `--help`。
