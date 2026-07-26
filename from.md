# 如何使用快思聪面板按钮触发 Home Assistant 自动化

要实现快思聪面板上空闲按钮触发 Home Assistant (HA) 中的自动化，这套集成已经专门提供了一个叫 `from_joins` 的功能。

它的核心原理是：当快思聪控制系统向 HA 发送指定 join 的状态变化时，HA 会直接执行配置好的脚本（Script）或服务（Service）。对于面板按键这种数字量（Digital Join），**系统内置了防抖功能，仅在 `0 → 1`（按下产生高电平跳变）时触发一次**，非常适合做面板按钮联动。

以下是具体的实现步骤：

## 1. 快思聪侧 (SIMPL Windows)
在你的快思聪 SIMPL 程序中：
1. 找到你空闲面板按钮的信号（比如按键 `Press` 信号）。
2. 把这个按键信号连接到与 HA 通信的那个 `XSIG`（Intersystem Communication）模块的一个**空闲数字输入管脚**上。
3. 记下这个管脚对应的 Join 编号，假设它是第 **100** 号数字 Join，那么在 HA 里它的代号就是 `d100`。

## 2. Home Assistant 侧配置
打开你的 HA 配置文件（`configuration.yaml` 或者通过 `xlsx_to_yaml.py` 生成的 `crestron.yaml`），在 `crestron:` 下面找到或添加 `from_joins:` 字段。

配置格式与 Home Assistant 的原生脚本（Script）语法完全一致：

```yaml
crestron:
  port: 10200          # 你的原端口
  from_joins:
    # --- 示例 1: 按下快思聪按键 d100，触发 HA 里的某个自动化 ---
    - join: d100
      script:
        - service: automation.trigger
          target:
            entity_id: automation.good_night_scene  # 替换成你 HA 里自动化的实体 ID

    # --- 示例 2: 按下快思聪按键 d101，直接开关 HA 里接入的小米台灯 ---
    - join: d101
      script:
        - service: light.toggle
          target:
            entity_id: light.xiaomi_desk_lamp

    # --- 示例 3: 还可以执行更复杂的一串动作 ---
    - join: d102
      script:
        - service: media_player.volume_set
          data:
            volume_level: 0.5
          target:
            entity_id: media_player.living_room_speaker
        - service: media_player.play_media
          target:
            entity_id: media_player.living_room_speaker
          data:
            media_content_id: "http://example.com/doorbell.mp3"
            media_content_type: "music"
```

## 3. 应用生效
修改完 YAML 后，在 Home Assistant 中点击**“重新启动”**（或重载相关配置），当快思聪面板上按下对应的按键时，HA 就会立刻执行你写在 `script` 里的动作。

**💡 小提示**：
* 配置里写的是 `script`，所以你可以用几乎所有 HA 支持的动作（调用服务、延迟 `delay`、甚至根据条件 `choose`）。
* 虽然叫 `from_joins`，但它的效果就相当于 HA 中的 Trigger（触发器），不需要在 HA 的“自动化”界面里再专门去写 webhook 或 MQTT 监听，配置起来非常直接。
