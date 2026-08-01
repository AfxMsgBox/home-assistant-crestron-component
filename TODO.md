# TODO — `dev` 工作区当前待办

基线：`dev` / `8bf40d2` 加当前未提交修改。本轮为全量通读代码后的复审结论。

## 已定型的决策（不要再改）

- **unique_id 规则在 0.4.0 正式发布后冻结**。
- **集成不做 unique_id 迁移**，运行时不读取、修改或删除 HA 实体注册表。开发阶段
  每次测试都是「删除集成 → 重新添加」，不存在遗留实体需要迁移；发布后规则不变，
  也就不会再产生需要迁移的场景。
- 组 ID（select / mode sensor）使用**完整 join 组升序拼接**，例如
  `crestron_select_512_513_514`。

## 上一轮已完成（已验证）

301 个测试通过、mypy 通过、转换器输出仍能通过新 schema、真实配置（264 实体 /
750 join）零回归。

- reload 时未知顶层键会拒绝重载并保留旧配置；首次启动仍只 warning。
- number 按 XSIG 能力限定为整数，真实零反馈不再丢失，恢复值受 min/max
  约束，小数配置和命令不会再被静默截断。
- media_player sources 只接受正整数或 ASCII 十进制字符串，整张 mapping
  归一化后检查编号和显示名重复。
- 连接接管测试已覆盖真实 TCP → FrameDecoder → FromJoinBridge → Script：
  新连接首次高电平不触发，随后真实 `0→1` 只触发一次。
- switch 的 `mode_joins` 状态与 `mode` 属性使用同一完整性判据：部分同步时不再
  提前显示“关闭”，全部上报且全低后才判关闭。
- README 与 ARCH 已同步上述行为。

---

# 本轮新发现

## P1：行为矛盾，建议合并前修复

### 1. climate 没有跟上 number 刚立下的两条规则

**文件**：`custom_components/crestron/climate.py`

number 这一轮明确了两条语义，climate 作为温度的主要入口两条都还是旧行为，同一个
值走两个平台结果相反。

**1a. 温度设定静默截断**（`climate.py:353`）

```python
self._hub.set_analog(self._set_temp_join, int(temp))
```

`int(25.5)` → `25`。number 现在会明确拒绝小数（"不能静默截断"），climate 仍然
悄悄改值。

**1b. 真实的 0 被当作「未上报」丢弃**（`climate.py:244`、`248`、`259`）

```python
v = self._hub.get_analog(self._reg_temp_join)
if v:                       # 0 在这里等于「没收到」
    self._reported_temp = v
```

实测：主控上报室温 `0` → `current_temperature` 保持 `None`；上报 `22` 才生效。
车库、露台这类位置 0 °C 是真实读数，而 `has_analog()` 已经存在——number 正是用它
修好的同一个问题。

**建议**：

- `async_set_temperature` 复用 number 的 `_whole_number()` 判定（建议提到共用
  模块，避免出现第三份实现）；小数拒绝而不是截断。
- 室温与设定值改用 `has_analog()` 判定是否上报过，上报过就接受 0。
- 补测试：设定 25.5 被拒、室温 0 °C 能上报。

### 2. reload 对未知顶层键收得过宽，形成单向死锁

**文件**：`custom_components/crestron/__init__.py:101`、`:287`

拼错 `lights:` 会删光所有灯，这个方向是对的。但当前实现是「**存在任何**未知顶层
键 → 拒绝 reload」，而 `async_setup()` 启动时只 warning。

实测：含未知键 `fan:` 的配置，HA 启动正常加载，`crestron.reload` **永远被拒绝**，
错误信息还提示是"拼写错误"。结果是一份能跑的配置再也无法热重载。

**建议**：判据从「存在未知键」收紧为「未知键导致某个**原本存在的平台段消失**」
——对比新旧配置，只有当某个平台在旧配置里有实体、新配置里整段不见时才拒绝。其余
未知键维持 warning，与启动保持一致。

---

## P2：测试与清理

### 4. 独立反馈 join 的测试覆盖仍不完整

**文件**：`tests/test_switch_feedback.py`、`tests/test_onoff_light.py`、
`tests/test_climate_filter.py`

switch FakeHub 已补 `has_digital()`，`mode_joins` 的部分同步/高电平/全低路径也已
覆盖；仍缺 switch 和 light 的 `state_join` / `switch_join` 用例。light FakeHub
还没有 `has_digital()`，测试这些分支时会先抛 `AttributeError`。

climate 当前不需要 `has_digital()`；修复上面的温度零值问题时，应给它的 FakeHub
补 `has_analog()`，并把现有“零值被丢弃”测试反转为“真实 0 °C 正常上报”。

### 5. `join_uid()` 已是死代码，但仍被测试维护

**文件**：`custom_components/crestron/entity.py:35`、
`tests/test_setup_platform_entities.py`（10 处引用）

unique_id 现在全部由 `unique_ids.py` 生成，`join_uid()` 在 production 里**没有
任何调用者**，只剩测试在测它。

**建议**：删除 `join_uid()` 与对应的 `JoinUidTests`。它要防的「模拟/数字编号空间
混用」问题，已由 `unique_ids.py` 各平台显式的 `d` 前缀和 `duplicate_unique_ids()`
覆盖。

### 6. 未使用的导入

- `custom_components/crestron/bridge.py:25` — `CONF_TO_HUB`、`CONF_FROM_HUB`
- `custom_components/crestron/join_registry.py:25` — `Optional`

---

## 记录：number 的读写范围不对称（暂不改，但应补进文档）

`async_set_native_value()` 拒绝超出 `min`–`max` 的写入，恢复值也检查范围，但
**实时反馈不检查**——主控上报 0 时 `min: 16` 的实体会显示 0。

方向应该是对的（反馈是事实，不该被夹断），但三条路径的严格程度不一致，README
目前没有说明。建议在 Number 一节补一句：范围约束作用于写入与恢复，主控回传的
真实值原样显示。

---

## 暂缓项

以下项目需要先确定运行语义或扩大产品范围，不在本轮直接改动：

1. 出站 socket 写入没有有界队列和真正背压。需要先确定队列上限、满载时合并/
   丢弃策略、重连后的重放规则，并处理 writer 生命周期与顺序保证。
2. `from_joins` 后台脚本任务没有集中跟踪、取消和并发限制，并共用一个
   `Context`。需要决定同一 join 是否串行、不同 join 的并发上限，以及 reload/
   stop 时正在执行的 HA 脚本应取消还是允许完成。
3. `set_serial` 超长时整帧丢弃。改为字节截断可能切断 UTF-8 字符或改变控制系统
   语义，需先确认主控端期望。
4. xlsx 转换器只生成灯光、插座、窗帘和空调。是否扩展 number/select/sensor/
   binary_sensor/media_player，需要先定义 sheet、表头和示例工作簿格式。
