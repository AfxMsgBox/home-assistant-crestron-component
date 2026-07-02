# Crestron 集成架构优化 TODO

本文记录当前代码架构**尚未实施**的优化方向。目标是在保持现有行为兼容的前提下，
降低重复代码、明确职责边界、提升状态同步可靠性与测试可维护性。

（P0 全部、P1-7/P1-8/P1-9、P2-11/P2-13 已完成，相关说明见 git 历史。）

## 暂不实施 / 远期参考

### JoinRef / JoinEvent 统一模型（暂不实施）

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

并提供 `JoinRef.parse("d12")` / `JoinRef.digital(12)` / `join.key == "d12"` 等。

> **暂不实施**：收益最不确定，全链路改动大；若长期维持「新模型 + 旧
> `(cbtype, value)` 字符串 API」双轨反而增加复杂度。保留作为远期参考，
> 等其它项稳定后再单独评估。

## 较大重构 / 后续增强

### 连接管理器与可控写入缓存策略（暂缓）

当前无连接时 `_write()` 会直接丢弃数据并记录日志。可将连接生命周期抽成
connection manager：当前 writer、available 状态、on_connect/on_disconnect
hooks、可选 pending write policy。

写入缓存策略应谨慎设计：

- 点动命令不应在重连后 replay，避免误动作
- `to_joins` 这类状态反馈可以考虑保存最后值，重连后 flush

> **暂缓**：写缓存的收益（重连后回填状态）已被现有 sync-all 机制覆盖（主控重连
> 发 `0xFB` 触发 `to_joins` 重发）；再加一层写缓存重复造轮子，还平白引入「哪些
> 能 replay」的误动作风险。结构性拆分可做可不做，写缓存不建议做。

### Climate capability 化（暂缓）

`CrestronAC` 当前包含电源、温度、运行模式、风速、恢复状态、settle window、
温度过滤等逻辑。若后续空调功能继续扩展，可拆成 capability：

- `ClimatePowerCapability`
- `ClimateTemperatureCapability`
- `ClimateModeCapability`
- `ClimateFanCapability`

每个 capability 负责：subscribed joins、supported features、read state、
write command。

> **暂缓**：现在只有一种空调协议、功能已收敛且有 `test_climate_filter` 保护。
> 无第二类设备/新需求时拆分只会把复杂度从「一个类」搬到「四个类 + 组合」，
> 总量不降反升。等出现第二类空调协议、抽象边界清晰后再拆。

## 性能与架构进阶优化 (待评估/实施)

### 1. 事件分发（Dispatch）并发优化
**问题**：`_dispatch` 当前是串行 `await` 广播。系统冷启动/断网重连发送 `AVAILABLE_KEY` 事件时，会串行阻塞 `_handle_frame` 导致 TCP 接收循环延迟。
**优化方案**：使用 `asyncio.gather` 或 `hass.async_create_task` 包装 `_safe_call`，将状态更新抛入后台并发执行。

### 2. 避免高频字符串拼接
**问题**：核心循环 `await self._timed_dispatch(f"d{frame.join}", str(frame.value), stats)` 每次都在产生新字符串，在瞬时海量包（如冷启动全量 Sync）时增加垃圾回收压力。
**优化方案**：快思聪 Join 数是固定的（Digital 4096，Analog 1024），可在模块初始化时预分配 Join 字符串字典或数组，如 `_DIGITAL_JOIN_STRS = [f"d{i}" for i in range(4097)]`，直接按索引取用。

### 3. God Object 拆分解耦与事件总线
**问题**：`CrestronXsig` 职责过多（TCP 连接管理、XSIG 缓存、分发器），且内部 `_join_callbacks` 强耦合。
**优化方案**：考虑使用 Home Assistant 原生的 `async_dispatcher_send` 代替手写的 Callback Set；分离 TCP Connection Manager 和 Protocol Handler。

## 注意事项

- 所有重构应保持现有 YAML 配置兼容。
- 点动命令不能随意缓存或重放，避免设备误动作。
- 状态同步依赖 Crestron 主控主动回传；HA 不能查询单根 join。
- 每一步重构都应配套单元测试，优先覆盖协议编解码、状态策略和 bridge 行为。
