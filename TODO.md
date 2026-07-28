# TODO — 代码待办

通读全量源码后整理的问题清单。标注 **[已复现]** 的条目附了可运行的复现步骤；标注 **[潜在]** 的是当前配置还碰不到、但规模变大后会踩的坑。

README 是按**代码现状**写的（不是按理想状态），所以修完下面的条目要回头同步 README 对应说明。

> 编号沿用最初的清单，**不重新编号**，以免和历史讨论/提交信息对不上。所以数字是不连续的，看状态请以下面两张表为准。

---

## 📊 一览

### 🔴 待办（10 项）

按建议动手顺序分批，同一批可以一起收拾。

| 批次 | # | 问题 | 优先级 | 主要文件 |
|---|---|---|---|---|
| **① 先做** | [2](#2-ci-每次-push-必红workflow-依赖两个已被删除的文件-已复现) | CI 每次 push 必红 **[已复现]** | P0 | `.github/workflows/tests.yml` |
| **① 先做** | [3](#3-gitignore-被删pycache-会被误提交) | `.gitignore` 被删，`__pycache__` 会被误提交 | P0 | 仓库根 |
| **② 静默失败** | [8](#8-_to_int-只认纯数字串join-号带小数点就被静默丢弃-潜在) | `_to_int` 吞掉带小数点的 join 号 **[潜在]** | P2 | `tools/xlsx_to_yaml.py` |
| **④ 一致性收尾** | [14](#14-number--select-的恢复逻辑与-switch--light-不一致) | `number`/`select` 恢复逻辑与 `switch`/`light` 不一致 | P2 | `number.py` / `select.py` |
| **④ 一致性收尾** | [15](#15-杂项各一两行可一起收拾) | 杂项 6 条（各一两行） | P2 | 多处 |
| **⑤ 按需** | [13](#13-_write-没有背压) | `_write()` 没有背压 | P2 | `crestron.py` |
| **⑤ 按需** | [16](#16-串行超长丢弃只记-info) | 串行超长丢弃只记 `info` | P2 | `crestron.py` |
| **⑤ 按需** | [17](#17-set_serial-超长时截断-vs-丢弃的取舍) | `set_serial` 超长：截断 vs 丢弃 | P2 | `crestron.py` |
| **⑤ 按需** | [18](#18-仓库地址不一致) | 仓库地址不一致 | P2 | `manifest.json` |
| **⑤ 按需** | [19](#19-media_player-等平台缺少批量生成支持) | `media_player` 等平台缺批量生成 | P2 | `tools/xlsx_to_yaml.py` |

### ✅ 已修复（9 项）

| # | 问题 | 原优先级 | 验证方式 |
|---|---|---|---|
| [1](#1-climate-实体不归属-ha-设备) | `climate` 实体不归属 HA 设备 | P0 | 单测 ✅ / 真机 ⏳ |
| [4](#4-settle-看门狗-typeerror) | settle 看门狗 `TypeError` | P1 | 单测 ✅（含回退验红） |
| [5](#5-选项对话框不打勾提交会无限重弹) | 选项对话框不打勾提交会无限重弹 | P1 | 仅代码审查 / 真机 ⏳ |
| [6](#6-连接建立时不下发-to_joins) | 连接建立时不下发 `to_joins` | P1 | 单测 ✅（4 例） |
| [7](#7-unique_id-混用模拟数字编号空间) | `unique_id` 混用模拟/数字编号空间 | P2 | 单测 ✅（**无需实体迁移**） |
| [9](#9-重复的-join-键会静默覆盖) | 重复的 `join:` 键静默覆盖 | P2 | 单测 ✅（12 例）+ 真实 yaml 对照，无误报 |
| [12](#12-诊断下载不脱敏串行-join) | 诊断下载不脱敏串行 join | P2 | 单测 ✅（脱敏后仍可判断 join 是否上报过） |
| [10](#10-crestron-下的平台键写错不报错) | `crestron:` 下的平台键写错不报错 | P2 | 真实 yaml 对照，无误报 |
| [11](#11-sensor--binary_sensor-上报前返回-0--false) | `sensor`/`binary_sensor` 上报前返回 0/False | P2 | 单测 ✅（11 例）⚠️ 行为变更 |

⚠️ 提醒：**#11 是行为变更**，部署后留意；已修的这批在 CI 上**一次都没跑过**（因为 #2）。

---

## 🔁 本轮代码审查修复（无原编号）

外部审查发现、已修复并补测试的问题。列在这里以免和上面的编号体系混淆。

| 问题 | 影响 | 修复 |
|---|---|---|
| `from_joins` 不是真上升沿：只忽略 `"0"`，不记前值 | **每次连接/重连的 `0xFD` 全量上报会把所有当时为高的按键 join 当成按下**，重启即重放场景 | `bridge.py` 记录每根数字 join 的前值，只有 `0→1` 才触发；连接后首次上报仅建立基线。README 已说明取舍 |
| `pulse_digital` 无 `finally` | 0.2 s 保持期内被取消（重载/停机）→ **join 永久留在高电平**，继电器一直吸合 | 释放移入 `finally`；4 个平台共用此函数 |
| 平台 schema 无能力组合校验 | 手写 YAML 可生成无法控制的实体和 `crestron_light_onoff_None` | `light` 增加能力组合校验；`sensor`/`select`/`media_player` 拒绝空集合；`number` 校验 `min<max`、`step>0` |
| `vol.All(int, ...)` 放行 `bool` | `on_join: true` 变成回调键 `"dTrue"`，永不匹配 | `schema.py` 显式拒绝 `bool` |
| unique_id 迁移假定每项都是 dict | 一条格式错误的实体配置让**整个集成起不来**（迁移跑在平台校验之前） | 逐条跳过并告警；整体再包一层兜底 |
| 组 unique_id 可能碰撞 | 两个只读 mode sensor 共用最小 join → HA 静默丢弃其中一个 | 新增 `duplicate_unique_ids()`，启动/重载时告警 |
| reload 无回滚 | 新端口绑不上 → 配置项起不来，且旧配置已被覆盖 → **一个实体都不剩** | 先整体预校验并列出会被跳过的实体；加载失败自动回滚上一份配置 |
| reload 任何人可调用 | 重载会重绑端口、重建所有实体 | 改用 `async_register_admin_service` |
| 日志把帧数写成 join 数 | 「332 joins」实为 332 帧 | 改为「N frames covering M joins」 |
| `state_join` / `is_closed_join` / media_player 未用 `has_*` | 首次同步前把「未知」显示成关/开/0 | 未上报一律返回 `None` |
| mode sensor 过早判「关闭」 | 初始同步中第一根低电平 join 到达就报「关闭」 | 改为所有 mode join 都上报过才判定 |
| 转换器不转义换行/控制字符 | Excel 单元格里的换行会生成折叠或损坏的 YAML | `_yaml_scalar` 转义 `\n\r\t` 及 C0 控制字符 |
| 部署脚本直接重启 | 配置有错时 HA 起不来 | 重启前先跑 `ha core check`，不通过则中止 |

---

# 🔴 待办详情

## 批次 ① — P0，现在就是坏的

### 2. CI 每次 push 必红：workflow 依赖两个已被删除的文件 **[已复现]**

**文件**：`.github/workflows/tests.yml`（已提交）、`requirements-test.txt`、`mypy.ini`（**未提交**）

workflow 里有两步：

```yaml
- name: Install test dependencies
  run: pip install -r requirements-test.txt      # ← 文件不在仓库里
- name: Type-check pure-logic modules
  run: mypy --config-file mypy.ini               # ← 文件不在仓库里
```

但这两个文件被删过：

```
50dcf72 Delete mypy.ini
20e4b18 Delete requirements-test.txt
```

它们现在只以**未跟踪文件**的形式躺在工作区（`git status` 里是 `??`），内容是完好的。CI 拉的是干净 checkout，所以第一步 `pip install -r requirements-test.txt` 直接失败，**后面的测试和类型检查根本没跑过**。

**修复**：

```bash
git add requirements-test.txt mypy.ini
```

**顺带解决**：这同时是「缺 voluptuous 时本地测试报错」的正解——`requirements-test.txt` 里已经写了 `voluptuous` 和 `mypy`，本地照着装一次即可：

```bash
pip install -r requirements-test.txt
```

**验证**：`git ls-files | grep -E 'requirements-test|mypy.ini'` 应当有输出；然后在干净 clone 里跑一遍 workflow 的三步。

> 注意 `.github/workflows/tests.yml` 工作区里还有一处**未提交的注释改动**（把「voluptuous 只有 test_schema.py 需要」改成「平台测试也要用真 voluptuous schema」）——那个新注释才是对的，一并提交。

### 3. `.gitignore` 被删，`__pycache__` 会被误提交

**文件**：仓库根（`469eb02 Delete .gitignore`）

`git status` 现在有三个未跟踪的 `__pycache__/`（`custom_components/crestron/`、`tests/`、`tools/`）。任何人一次 `git add -A` 就会把编译产物提交进去。

**修复**：恢复一个最小 `.gitignore`：

```gitignore
__pycache__/
*.py[cod]
.mypy_cache/
```

---

## 批次 ② — 配置类静默失败

两条都只需加告警或放宽解析，改动都很小。

### 8. `_to_int` 只认纯数字串，join 号带小数点就被静默丢弃 **[潜在]**

**文件**：`tools/xlsx_to_yaml.py:100-102`

```python
def _to_int(value):
    value = (value or "").strip()
    return int(value) if value.isdigit() else None
```

实测：

| 输入 | 结果 |
|---|---|
| `'505'` | `505` ✅ |
| `'505.0'` | `None` ❌ |
| `'-1'` | `None` |
| `'5e2'` | `None` |

Excel 把单元格存成数值格式时，XML 里出现 `505.0` 是完全可能的（改过单元格格式、经第三方工具另存、公式缓存值）。一旦发生，**该 join 被静默当成「没填」**：灯少一个色温、空调少一档风速、整行被跳过——终端只会打一句 `skipped N placeholder/empty row(s)`，没有任何指向具体行的报错。

你当前这份 xlsx 存的是整数，所以现在没事。

**修复**：容忍小数与负号，并对「填了但解析不出来」的格子出声：

```python
def _to_int(value):
    value = (value or "").strip()
    if not value or value == "//":
        return None
    try:
        f = float(value)
    except ValueError:
        return None          # 真·占位符
    if f != int(f):
        return None          # 425.5 这种不是合法 join
    return int(f)
```

再在 `generate()` 里把「非空但解析失败」的格子按 sheet+行号打印出来。

### 9. 重复的 `join:` 键会静默覆盖

**文件**：`custom_components/crestron/bridge.py:68`（`to_joins`）、`:133`（`from_joins`）

```python
self._join_to_template[join] = template      # 同一个 join 写两遍 → 后者胜
self._scripts[entry[CONF_JOIN]] = Script(...)
```

`to_joins` 里把 `d12` 写两遍（复制粘贴配置时很常见），前一条被无声丢掉。`from_joins` 同理，同一个面板按键绑两个脚本时只有最后一个生效。

**修复**（已完成）：新增 `join_registry.py`，在 `async_setup` 与 `crestron.reload` 时检查整份配置的 join 归属，重复的 `to_joins`/`from_joins` 键会明确报「只有最后一条生效」。同一个检查还覆盖了原计划之外的一类问题：两个实体（或一个实体与一条 `to_joins`）写同一根 join。只读镜像（`sensor` 的 `mode_joins` 跟着空调模式走等）不算冲突，对本仓库 264 实体 / 750 join 的真实配置零误报。结果也进了诊断下载的 `join_usage`。

`bridge.py` 仍然是「后者胜」——检测到并报出来即可，改成一个 join 触发多个脚本属于功能增强，未做。

---

## 批次 ③ — 隐私

### 12. 诊断下载不脱敏串行 join

**文件**：`custom_components/crestron/crestron.py:280-300`、`custom_components/crestron/diagnostics.py`

`diagnostics()` 原样导出 `serial` 缓存全部内容。串行 join 常用来推送天气、门禁姓名、日程等文本，而诊断包的用途就是**发给别人排障**。

**修复**（已完成）：`diagnostics(redact=True)` 默认把串行正文换成 `<N chars redacted>`、把对端地址换成 `**REDACTED**`。join 号、字符数和 `cache_counts` 保留——判断「这根 join 报没报过」只需要这些。数字/模拟量是电平和数值，原样保留。README「下载诊断」一节已说明。

---

## 批次 ④ — 一致性与收尾

### 14. `number` / `select` 的恢复逻辑与 `switch` / `light` 不一致

**文件**：`custom_components/crestron/number.py:59-75`、`custom_components/crestron/select.py:53-65`

这俩是 `if 已连接: 读实时反馈 / else: 恢复重启前的值`——**二选一**。于是「已连接、但这根 join 主控还没推过」时，既不读反馈（读到 0/None 被当作未知丢弃）也不恢复，实体显示「未知」。

`switch.py:103-113` 和 `light.py:198-209` 用的是更好的顺序：**先恢复兜底，再用确定的实时反馈覆盖**。

**修复**：把 number/select 改成和 switch/light 一致的两段式。

### 15. 杂项（各一两行，可一起收拾）

- **`from_joins` 所有脚本共用一个 `Context`**（`bridge.py:130`）：`self.context = Context()` 建一次就反复用。HA 惯例是每次运行给一个新 Context，否则自动化追踪里所有面板按键触发看起来都是同一个来源，还可能干扰 HA 的循环检测。改成在 `_run_script` 里 `Context()`。
- **卸载时 `hass.data` 残留**（`__init__.py:115-122`）：只 `pop(HUB_WRAPPER)`，`HUB` 和 `YAML_CONF` 留着。重载会被新值覆盖，不算泄漏，但 `HUB` 指向已停掉的 `CrestronXsig`，谁在这个窗口里读它会拿到僵尸对象。
- **`remove_callback` 留空集合**（`crestron.py:93-96`）：遍历所有 join 的集合做 discard，空集合本身不删。实体反复增删会慢慢堆积空 set。量级极小。
- **`encode_analog` 的 join 高位没掩码**（`xsig_protocol.py:74`）：`(join - 1) >> 7` 未与 `0b111` 相与，join > 1024 会溢出到 bit3（串行判别位）。调用方 `set_analog` 和 schema 都已挡住越界，属于纵深防御。
- **`stop()` 绕过了 `_notify_available`**（`crestron.py:62-63`）：直接 `_available = False` + `_dispatch`，即使本来就是 False 也会广播一次。无害，但和「可用性去抖」的设计不一致。
- **`strings.json` 是中文**：HA 约定 `strings.json` 为**英文源**、`translations/en.json` 由它生成。现在 `strings.json` 与 `zh-Hans.json` 内容相同（中文），意味着非中英语系用户回退到的是中文。把 `strings.json` 换成英文即可。

---

## 批次 ⑤ — 按需

### 13. `_write()` 没有背压

**文件**：`custom_components/crestron/crestron.py:327-334`

```python
def _write(self, data):
    ...
    self._writer.write(data)     # 从不 await writer.drain()
```

`set_*` 都是同步方法（被同步的实体属性/回调调用），没法 await。正常流量下无所谓，但 `sync_all()` 一次性下发上百个 join、或主控 TCP 接收窗口卡住时，asyncio 的发送缓冲会无上限增长。

**部分处理**：`sync_all()` 现在包在 `CrestronXsig.batched_writes()` 里，整轮全量下发合并成**一次** `writer.write()`（几百次小写入 → 1 次）。这消掉了「一次性下发上百个 join」这条触发路径，但**没有**引入背压——主控接收窗口卡住时缓冲仍会增长。

**剩余修复**：low 成本方案是加个 `writer.transport.set_write_buffer_limits()` + 在缓冲超限时打 warning；彻底方案是改成异步发送队列（改动大，收益不明确）。**当前规模下不急**，记一笔。

### 16. 串行超长丢弃只记 `info`

**文件**：`custom_components/crestron/crestron.py:365`

默认 `logger: default: warning` 下这条完全不可见，用户会以为帧发出去了。旁边的 join 越界都是 `_LOGGER.warning`。**建议改成 `warning`**：数据被丢弃属于异常，不是常规信息。

同一函数 `crestron.py:329` 的「没有连接，发不出去」保持 `info` 是合理的（主控没连时会疯狂刷屏）。

### 17. `set_serial` 超长时「截断 vs 丢弃」的取舍

**文件**：`custom_components/crestron/crestron.py:363-369`

目前超 252 字节整帧丢弃。对「当前天气：…」这类 `to_joins` 串行推送，丢弃意味着面板一直显示旧值。可以考虑按 UTF-8 字符边界截断后发送（并打 warning），比整条丢掉更有用。**需要先确认主控侧对截断文本的容忍度**，属于行为变更，不要顺手改。

### 18. 仓库地址不一致

**文件**：`custom_components/crestron/manifest.json:5,7`

```json
"documentation": "https://github.com/zqyuan/home-assistant-crestron-component/blob/master/README.md",
"codeowners": ["@zqyuan"],
```

但实际 remote 与 `tools/update.sh:6` 都是 `AfxMsgBox/home-assistant-crestron-component`。HA 前端「文档」链接会指到一个可能不存在/不同步的仓库。

**修复**：三处统一。顺带：仓库没有 `hacs.json`，如果打算走 HACS 分发需要补。

### 19. `media_player` 等平台缺少批量生成支持

**文件**：`tools/xlsx_to_yaml.py:290-295`

`SHEET_BUILDERS` 只有 `灯光 / 插座 / 窗帘 / 空调`。`media_player`、`binary_sensor`、`number`、`select` 只能手写。如果 join 表里以后加了对应 sheet，需要补 builder。目前不是问题，记一笔。

---

# ✅ 已修复详情

> 这批改动都还在工作区**未提交**，且因为 #2，**在 CI 上一次都没跑过**。

### 1. `climate` 实体不归属 HA 设备

<sub>原 P0 · 验证：单测 ✅ / 真机 ⏳</sub>

`climate.py` 曾是九个平台里唯一没有 `from .device import device_info`、也从未设置 `_attr_device_info` 的，而 `tools/xlsx_to_yaml.py:284-285` 确实给每台空调生成了 `device_id` / `device_name`。因为 climate 的 schema 是 `extra=vol.ALLOW_EXTRA`，这两个键被静默吞掉，不报错也不生效——空调在 HA「设备」列永远是 `—`。

**改动**：

- `climate.py:55` 加 `from .device import device_info`；`climate.py:152` 加 `self._attr_device_info = device_info(config)`（紧跟 `_attr_unique_id`，与其余平台顺序一致）。
- `tests/test_climate_filter.py`：climate 现在会 import `.device` → 多 stub 一个 `homeassistant.helpers.entity`（`DeviceInfo=dict`，与 `test_switch_feedback.py:38` 等一致）；新增 `DeviceGroupingTests` 三例锁住行为。
- `README.md`：去掉两处 ⚠️ 标注，「设备分组」改回「所有平台都支持」。

**验证**：`python3 -m unittest test_climate_filter` → `Ran 12 tests ... OK`。**尚未在真实 HA 里跑过**——部署后请到 设置 → 设备与服务 → 设备 确认每台空调出现为独立设备。

### 4. settle 看门狗 `TypeError`

<sub>原 P1 · 验证：单测 ✅（含回退验红）</sub>

看门狗的判断条件从 `stats["first"] is not None` 改成 `stats["last"] is not None`（`crestron.py:257`）——`last` 非 None 蕴含 `first` 非 None，且 `last` 正是那个会被读到 `None` 的字段。

**验证**：新增 `tests/test_xsig.py::SettleWatchdogTests`，把 `SYNC_SETTLE_SECONDS` 临时压到 0.05s、注册一个 sleep 0.25s 的回调，用 loop exception handler 收集未处理异常。**把修复退回去跑过一遍，确认测试会红**（报出原始的 `TypeError: unsupported operand type(s) for -: 'float' and 'NoneType'`），再恢复。

### 5. 选项对话框不打勾提交会无限重弹

<sub>原 P1 · 验证：仅代码审查 / 真机 ⏳</sub>

`config_flow.py:51-63`：把「有没有提交」和「勾没勾」拆成两层判断，提交后无论如何都 `async_create_entry` 关掉对话框，只有勾选时才触发 `resync_to_joins()`。

**验证**：仅代码审查。config_flow 需要 stub 掉 `homeassistant.config_entries`，为一个三行分支搭这套 stub 收益不高。**改动是纯控制流，建议在真实 HA 里点一次确认**：打开选项 → 不勾 → 提交 → 对话框应当关闭且不触发重发。

### 6. 连接建立时不下发 `to_joins`

<sub>原 P1 · 验证：单测 ✅（4 例）</sub>

- `crestron.py`：新增 `register_connect_callback()`（与既有 `register_sync_all_joins_callback` 同构），在 `handle_connection` 发完 `0xFD`、置 available 之后调用；用 try/except 包住，一个坏模板不会拖垮本来正常的连接。
- `__init__.py:167-173`：`CrestronHub` 把 `self._sync_all` 同时注册到连接回调上。重发一个没变的值在协议层是幂等的，所以和主控紧接着发来的 `0xFB` 撞车无害。

**验证**：新增 `tests/test_xsig.py::ConnectCallbackTests` 四例——首次连接触发、**断开重连再次触发**、回调抛异常不影响连接（后续帧仍能解析、available 仍为真）、没注册回调也不出错。

> 原 TODO 里「HA 重启后是否也有这个缺口」取决于 `async_track_template_result` 的语义，**那部分我仍未实证**（本机装不了 HA）。不过本次修复覆盖的是更确定、也更常见的那一半：**每次连接建立都会推**，HA 重启后主控重连同样会走到这条路径，所以两种场景实际上都被兜住了。

### 7. `unique_id` 混用模拟/数字编号空间

<sub>原 P2 · 验证：单测 ✅ · **无需实体迁移**</sub>

**改法与原计划不同**，见下方说明。新增 `entity.py::join_uid(analog=(), digital=())`：模拟量候选保持**裸数字**，数字量候选加 `d` 前缀。两个空间就此不可能重叠，而**模拟量那一路的 id 与修复前完全一致**。

- `cover.py:102-105`：`join_uid(analog=(pos_join,), digital=(open_join, close_join))`
- `climate.py:150-156`：`join_uid(analog=(reg_temp_join, set_temp_join), digital=(on_join,))`

**为什么不按原计划加 `a`/`d` 双前缀**：那会把 `crestron_cover_480` 变成 `crestron_cover_a480`，**你现有的每一个 cover 和 climate 实体都会变成不可用孤儿**（entity_id、历史数据、自动化与看板里的引用全断），而当前配置里**根本没有实际碰撞**——纯粹是拿真实损失换一个假想收益。非对称方案同样根治了碰撞，代价为零。

**残留边界**（已接受）：两台空调若共用同一个模拟量 join（比如 A 的 `reg_temp_join` 和 B 的 `set_temp_join` 都是 415），仍会撞。但那本身就是配置错误——同一根模拟 join 不可能既是 A 的室温又是 B 的设定值。

**验证**：`tests/test_setup_platform_entities.py::JoinUidTests`（纯函数 5 例）、`test_cover_position.py::UniqueIdTests`、`test_climate_filter.py::UniqueIdTests`——都显式断言了「带模拟 join 的实体 id 保持裸数字不变」，把「不迁移」这条性质锁住了。

### 10. `crestron:` 下的平台键写错不报错

<sub>原 P2 · 验证：真实 yaml 对照，无误报</sub>

`__init__.py` 新增 `_KNOWN_CONFIG_KEYS`（`PLATFORMS` + `port`/`to_joins`/`from_joins`）与 `_warn_unknown_config_keys()`，在 `async_setup` 里对 `config[DOMAIN]` 的顶层键查一遍，不认识的打 warning 并列出合法键。**只警告不报错**，保留向前兼容余地。

**验证**：拿真实 xlsx 生成的 `crestron.yaml` 对照，其产出的 5 个顶层键（`port`/`light`/`switch`/`cover`/`climate`）全在白名单内，**无误报**；`lights` 这类 typo 会被抓到。

### 11. `sensor` / `binary_sensor` 上报前返回 0 / False

<sub>原 P2 · 验证：单测 ✅（11 例）· ⚠️ **行为变更**</sub>

改用 `CrestronXsig` 早就提供、但一直没人用的 `has_analog()` / `has_digital()` 谓词：

- `sensor.py:83-98`：模拟量 join 没被推送过 → `None`；`mode_joins` 一根都没报过 → `None`（报过而全低才是 `关闭`）。
- `binary_sensor.py:46-54`：join 没被推送过 → `None`。

**关键是要区分「没上报过」和「真的是 0」**——真实推送来的 `0` / `False` 仍然照常上报，不能一起吞掉。

**验证**：新增 `tests/test_sensor_unknown.py`（sensor / binary_sensor 此前**完全没有测试**），11 例覆盖三类：未上报→`None`、上报后正常读数、**真实的 0/False 不被误吞**。

> ⚠️ **行为变更，部署后留意**：这些实体在主控首次推送该 join 之前会显示「未知」而不是 0/off。如果你有自动化写的是 `is_state('binary_sensor.x', 'off')`，在 HA 刚启动、主控还没连上的窗口里将不再成立（改用 `is_state(..., 'on')` 取反，或加 `not is_state(..., 'unknown')` 判断）。这是**更正确**的行为——之前那个 `off` 是集成凭空断言的。
