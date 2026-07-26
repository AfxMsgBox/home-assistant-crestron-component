# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

本仓库面向中文用户，README、配置示例与代码注释大量使用中文，回复请保持中文。

## 这是什么

Home Assistant 自定义集成，通过 Crestron 控制系统的 **XSIG（Intersystem Communication）** 二进制协议，把 Crestron 的数字/模拟/串行 join 与 HA 实体双向打通。`custom_components/crestron/` 是要装进 HA 的集成本体；`tools/xlsx_to_yaml.py` 是离线配置生成器；`README.md` 是面向终端用户的完整文档（字段、接线、安装），写代码前值得通读对应小节。

## 常用命令

```bash
# 运行全部测试（纯逻辑层，不需要 Home Assistant 运行时）
python3 -m unittest discover -s tests

# 跑单个测试文件 / 单个用例
python3 -m unittest tests.test_xsig
python3 -m unittest tests.test_xsig.TestName.test_method

# 离线生成 crestron.yaml（纯标准库，不在 HA 内运行）
python3 tools/xlsx_to_yaml.py <join表.xlsx> <输出目录>
```

- 测试依赖：仅 `tests/test_schema.py` 需要 `voluptuous`（未安装会自动跳过）；其余为纯标准库。CI（`.github/workflows/tests.yml`）在 Python 3.11/3.12/3.13 上跑 unittest。
- 没有 lint/format 配置；保持与周边代码一致的风格即可。

## 测试为什么要绕开 `__init__.py`

实体级模块（light/switch/climate/...）`import homeassistant`，裸环境装不了，所以**测试只覆盖不依赖 HA 运行时的纯逻辑**：`crestron.py`（XSIG 协议）、`value_coercion.py`、`schema.py`、`tools/xlsx_to_yaml.py`。

`tests/loader.py` 是关键：它把这些模块挂到一个合成包 `crestron_under_test` 下单独加载，让 `schema.py` 的 `from .crestron import ...` 相对导入能解析，**同时绝不执行真正的 `__init__.py`**。新增纯逻辑测试时沿用 `loader.load("模块名")`，不要直接 `import custom_components.crestron`。实体级测试需要 `pytest-homeassistant-custom-component`，本仓库未集成。

## 架构

数据流贯穿三层，改任何一层前先理解它在链路里的位置：

1. **`crestron.py` — `CrestronXsig`（协议 + TCP，不依赖 HA）**
   - **HA 是 TCP server**，Crestron 主动连入。新连接会替换旧连接；只有"当前活跃连接"才有权清 writer 并广播不可用（见 `handle_connection` 的 `was_active` 逻辑）。
   - 所有 join 状态缓存在内存字典 `_digital/_analog/_serial`。`get_*` 只读缓存，**从不发网络请求**。
   - 按字节首位掩码解析三类帧（数字 2 字节 / 模拟 4 字节 / 串行变长以 `0xFF` 结尾）。控制字符：连接建立时 HA 发 `0xFD` 请求全量上报；收到 `0xFB` 触发 `sync_all_joins_callback` 重发所有 `to_joins`。
   - 任一 join 变更经 `_dispatch` 分发给回调。回调集合在 await 前**先快照成元组**（迭代中实体增删会改集合）；每个回调用 `_safe_call` 包裹，单个回调异常不会拖垮 TCP 读循环。

2. **`__init__.py` — `CrestronHub`（连接 HA 与协议层）**
   - `async_setup` 把 YAML 存进 `hass.data`，并发起一个 `SOURCE_IMPORT` config flow。**实体定义留在 YAML 里**（`crestron:` 之下），但走 config entry 建立——因为 HA 只为 config-entry 式集成建设备（`device_info` 对老式 YAML 平台无效）。`config_flow.py` 只负责建这个单实例 entry。
   - `to_joins`：每条统一包成 HA `Template`，用 `async_track_template_result` 监听，结果变化即经 `_set_join` → `value_coercion` → `set_*` 推给控制系统。
   - `from_joins`：注册按 join 过滤的回调，匹配后运行 HA `Script`（`value` 变量=join 值）。数字 join 仅在 `1`（上升沿）触发，避免点动按钮跑两次；脚本放后台跑且异常隔离。

3. **平台实体（`light.py`/`switch.py`/`climate.py`/`cover.py`/...）**
   - 统一模式：`async_added_to_hass` 里 `hub.register_callback(self.process_callback, joins=[...])`，回调体就是 `async_write_ha_state()`；`async_will_remove_from_hass` 里 `remove_callback`。
   - 全部 `_attr_should_poll = False`、`available` 跟随 `hub.is_available()`。
   - 模拟 join 的 0–65535 在各平台内部映射到业务单位（亮度 0–255、音量 0–1、温度原值等）。**窗帘位置例外**：主控直接用 0–100 模拟量表示百分比，`cover.py` 直读直写不做 /65535 缩放。

辅助模块：`schema.py`（join 编号/`join_key` 校验器，配置加载期就拒绝越界与格式错误）、`value_coercion.py`（模板结果 → XSIG 值的纯函数）、`device.py`（`device_id`/`device_name` → `DeviceInfo`，相同 `device_id` 的实体归一台设备）、`const.py`（所有 `CONF_*` 键名）。

## 改动时容易踩的协议/同步坑

- **没有"查询单根 join"的指令**。状态同步完全是推送式：唯一一次主动要状态是连接时发 `0xFD`（全量）。若主控程序没给某 join 配"状态回传"，HA 永远无法得知其真实状态——这是配置问题，代码侧无法弥补。
- **command-only join 用回传电平判定状态**：例如只开关灯读 `on_join`/`off_join` 的回传电平（一高一低才确定，都低/都高维持当前）；空调电源读 `on_join` 回传电平。
- **乐观状态 + 跨重启恢复**：点动类实体（如 `CrestronOnOffLight`）下命令后先乐观显示并用 `RestoreEntity` 跨重启恢复，回传到达后再以回传校正。纯瞬动（无 `state_join`）重启后会丢真实状态。
- **窗帘位置的优雅降级**（`cover.py`）：配了 `pos_join` 但主控没回传位置时（快思聪侧常见缺口），位置降级为命令推断（开→100/关→0/停→保持），`supported_features`/`assumed_state` 做成动态属性——无真实反馈时不暴露 `SET_POSITION`（不给滑块）。真实位置一到，`current_cover_position` 立即改用它、滑块自动恢复，`process_callback` 里清掉乐观值。同样用 `RestoreEntity` 跨重启恢复。
- **调光灯关灯的两步时序**（`light.py` `async_turn_off`）：先把当前电平原样重发、`sleep(OFF_REASSERT_SECONDS)`、再写 0，凑出一次"高→0"跳变。两步之间的延时是必须的——零延时会让两个模拟量落进同一控制系统扫描周期、0 被覆盖，导致要按两次才关。点动脉冲固定 `PULSE_SECONDS = 0.2`。
- **串行长度按 UTF-8 字节算**（≤252 字节，约 84 个汉字），超长丢弃。Join 编号越界写入运行期丢弃，schema 也会在加载期拒绝。

## 关键约定

- 用户配置全部写在 `configuration.yaml` 的 `crestron:` 之下（含 `port`、`to_joins`、`from_joins` 和各平台键 `light:`/`climate:`/...），**不是**顶层 `light: - platform: crestron`。原因见上：只有 config-entry 路径才能建设备。
- 改了实体的 `unique_id` 规则会让旧实体变成不可用孤儿（README 末尾有迁移说明）。
- 版本号在 `manifest.json` 与 README 顶部两处，发布时一起改。
