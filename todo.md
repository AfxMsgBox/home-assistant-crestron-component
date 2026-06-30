# Crestron 集成架构优化 TODO

本文记录当前代码架构可以继续优化的方向，按优先级分阶段推进。目标是在保持现有行为兼容的前提下，降低重复代码、明确职责边界、提升状态同步可靠性与测试可维护性。

## P0：低风险、高收益

### 1. 抽象平台实体基类 / mixin

当前多个平台实体都重复实现了以下模式：

- `_attr_should_poll = False`
- `async_added_to_hass()` 注册 join 回调
- `async_will_remove_from_hass()` 移除回调
- `process_callback()` 调用 `async_write_ha_state()`
- `available` 返回 `hub.is_available()`
- 初始化时设置 `device_info(config)`

建议新增 `CrestronEntityBase` 或 mixin，统一处理：

- hub 保存
- device_info 注入
- available 属性
- callback 注册 / 移除
- 默认 join 事件处理

优先迁移简单平台：`binary_sensor`、`sensor`、`media_player`，再逐步迁移 `number`、`select`、`switch`、`cover`、`light`、`climate`。

### 2. 拆分 `CrestronHub` 的 bridge 职责

`CrestronHub` 当前同时负责：

- 创建和持有 `CrestronXsig`
- 启停 TCP server
- 管理 `to_joins` template tracker
- 管理 `from_joins` script
- template 结果到 join 值的转换
- 响应 Crestron 的 sync-all 请求

建议拆出：

- `ToJoinBridge`：负责 HA template/entity state -> XSIG join
- `FromJoinBridge`：负责 XSIG join -> HA script
- `CrestronRuntime` 或继续保留较薄的 `CrestronHub`：只负责生命周期和组合上述组件

这样可以让 `__init__.py` 回归 Home Assistant lifecycle glue，降低单类复杂度。

### 3. 统一 pulse helper

当前多个实体都有类似的 0.2 秒点动逻辑：

- on/off light
- switch pulse mode
- cover open/close/stop
- climate power on/off

建议抽出通用 helper，例如：

```python
class PulseCommand:
    async def pulse(self, join: int) -> None:
        ...
```

或函数：

```python
async def pulse_digital(hub, lock, join, seconds=0.2):
    ...
```

注意保持每个实体独立 lock，避免同一设备并发点动冲突。

### 4. 统一 set-one-clear-others helper

当前以下场景都需要“选中一个 join，同时清掉其它 join”：

- climate 运行模式
- climate 风速
- select 选项

建议新增 helper：

```python
def set_one_clear_others(hub, joins: Iterable[int], target: int) -> None:
    for join in joins:
        hub.set_digital(join, join == target)
```

收益是减少重复逻辑，并保证多个平台行为一致。

### 5. 增加本地测试依赖文件

当前 CI 会安装 `voluptuous` 后运行 unittest，但仓库没有明确的本地测试依赖文件。建议新增：

- `requirements-test.txt`

至少包含：

```text
voluptuous
```

后续可视情况增加 Home Assistant 测试相关依赖或 mock 依赖说明。

## P1：中风险、中高收益

### 6. 引入统一 JoinRef / JoinEvent 模型

当前 join 表示方式分散：

- 协议层缓存用裸 int
- 回调用字符串，如 `d12`、`a3`、`s4`
- 配置中的 `to_joins/from_joins` 也使用字符串 join key
- 平台实体内部大多使用裸 int

建议新增结构化模型：

```python
@dataclass(frozen=True)
class JoinRef:
    kind: Literal["d", "a", "s"]
    number: int

@dataclass(frozen=True)
class JoinEvent:
    join: JoinRef
    value: bool | int | str
```

并提供：

- `JoinRef.parse("d12")`
- `JoinRef.digital(12)`
- `JoinRef.analog(3)`
- `JoinRef.serial(4)`
- `join.key == "d12"`

迁移策略：先内部引入，同时兼容旧的 `(cbtype, value)` callback API。

### 7. 拆分 XSIG 协议编解码为纯函数 / 纯模块

`CrestronXsig` 当前同时负责 TCP server、连接生命周期、帧解析、帧编码、状态缓存、回调分发。建议逐步拆出：

- `encode_digital(join, value) -> bytes`
- `encode_analog(join, value) -> bytes`
- `encode_serial(join, value) -> bytes`
- streaming decoder 或 frame parser

收益：

- 协议测试不需要启动 TCP server
- 更容易覆盖跨 read 分片、非法 UTF-8、未知帧等边界条件
- 后续如需 mock transport 或其它 transport，可复用协议层

### 8. 明确 unknown 与真实 0/False 的区别

当前底层 getter 默认返回：

- analog 未知 -> `0`
- digital 未知 -> `False`
- serial 未知 -> `""`

这会让“尚未收到状态”和“真实值为 0/False/空字符串”混在一起。部分平台已用业务逻辑规避，例如 number 把 0 当作 unknown，但这不是统一模型。

建议增加 API：

```python
def has_analog(join: int) -> bool: ...
def get_analog(join: int, default=None): ...
def has_digital(join: int) -> bool: ...
def get_digital(join: int, default=None): ...
def has_serial(join: int) -> bool: ...
def get_serial(join: int, default=None): ...
```

实体层可以逐步迁移到显式 unknown 语义。

### 9. 改进配置加载错误隔离

每个平台目前通常直接：

```python
items = hass.data[DOMAIN][YAML_CONF].get("platform", [])
async_add_entities(Entity(hub, PLATFORM_SCHEMA(item)) for item in items)
```

建议新增统一 helper：

```python
def load_platform_entities(hass, platform_key, schema, factory):
    ...
```

可做到：

- 单个实体配置错误只跳过该实体
- 日志格式统一
- 各平台 `async_setup_entry()` 更短

## P2：较大重构 / 后续增强

### 10. 引入连接管理器与可控写入缓存策略

当前无连接时 `_write()` 会直接丢弃数据并记录日志。建议将连接生命周期抽成 connection manager：

- 当前 writer
- available 状态
- on_connect hooks
- on_disconnect hooks
- 可选 pending write policy

写入缓存策略应谨慎设计：

- 点动命令不应在重连后 replay，避免误动作
- `to_joins` 这类状态反馈可以考虑保存最后值，重连后 flush

### 11. 增强 diagnostics / options flow

当前集成是 YAML-first，这是合理的，因为 Crestron join 表通常很大。但可以通过 diagnostics 或 options flow 提升可观测性：

- 当前连接状态
- 最近连接 peer
- TCP port
- 已加载实体数量
- digital/analog/serial 缓存数量
- 手动触发 `to_joins` 重新同步

### 12. Climate capability 化

`CrestronAC` 当前包含电源、温度、运行模式、风速、恢复状态、settle window、温度过滤等逻辑。若后续空调功能继续扩展，可以拆成 capability：

- `ClimatePowerCapability`
- `ClimateTemperatureCapability`
- `ClimateModeCapability`
- `ClimateFanCapability`

每个 capability 负责：

- subscribed joins
- supported features
- read state
- write command

这属于较大重构，建议在 P0/P1 稳定后再做。

### 13. 增加类型标注与 Protocol

建议从纯模块开始逐步增加类型标注：

- `value_coercion.py`
- `schema.py`
- 协议 encode/decode 模块
- `device.py`

再逐步扩展到 `CrestronXsig` 和平台实体。重点标注 async callback 类型、join value 类型、配置 dict 类型。

## 推荐推进顺序

1. 新增测试依赖文件，确保本地测试可稳定运行。
2. 抽平台实体基类，从简单平台开始迁移。
3. 抽 pulse helper 和 set-one-clear-others helper。
4. 拆 `to_joins/from_joins` bridge。
5. 引入 JoinRef / JoinEvent，并兼容旧 callback API。
6. 拆 XSIG 协议编解码纯函数。
7. 处理 unknown 与 0/False 语义。
8. 最后再考虑连接管理器、diagnostics、climate capability 化。

## 注意事项

- 所有重构应保持现有 YAML 配置兼容。
- 点动命令不能随意缓存或重放，避免设备误动作。
- 状态同步依赖 Crestron 主控主动回传；HA 不能查询单根 join。
- 每一步重构都应配套单元测试，优先覆盖协议编解码、状态策略和 bridge 行为。
