# Synthesizer V Studio 2.3.0 能力核对

核对日期：2026-11-10。用户报告的编辑器 2.3.0 版本已通过实际桥接只读核实；以下运行时能力仍以实际读回为准，不能以版本号代替接口调用成功。

## 官方更新与接口依据

[2.3.0 正式更新记录](https://dreamtonics.com/synthesizer-v-studio-2-pro-2-3-0-update-and-improvements/)发布于 2026-11-02。相关变化包括音素系统及 X-SAMPA、法语/德语/巴西葡语/意大利语、参数随音符移动，以及音高绘制性能与位置修复。部分新功能需要 v205 声库；官方当时仅确认 Dreamtonics 声库更新，不能推断所有合作声库已经具备相同能力。本项目本轮不改写音素、声库文件或编辑器设置。

官方脚本手册当前列出的能力：

| 数据或操作 | 官方接口 | 本项目含义 |
| --- | --- | --- |
| 真实连续音高对象 | [PitchControlCurve](https://resource.dreamtonics.com/scripting/PitchControlCurve.html) | `pitchCurve` 使用宿主原生曲线，不以 `pitchDelta` 的密集点假装原生绘制 |
| 已计算演唱音高 | [SV.getComputedPitchForGroup](https://resource.dreamtonics.com/scripting/SV.html#getComputedPitchForGroup) | 只读 MIDI 半音采样；空结果为未就绪，缺帧保留 null |
| 通用参数与声线自动化 | [Automation](https://resource.dreamtonics.com/scripting/Automation.html#getDefinition) | 按实际参数定义、值域和本地调整上限预览；新增补齐 `voicing` |
| 声线默认设置 | [NoteGroupReference.getVoice](https://resource.dreamtonics.com/scripting/NoteGroupReference.html#getVoice) | 读取 `vocalModeParams` 已有名称及 pitch/timbre/pronunciation 三维值 |

未在当前公开手册找到“枚举声库全部声线名称”的接口。因此返回的 `vocalModes.complete` 明确为 `false`。目录由主组/当前组已有声线设置和用户核对的补充名称构成，不扫描或解包声库文件，也不按歌手名称猜测 Airy、Chest、Cute 是否存在。

## 本轮具体修复

- 兼容 Lua 宿主的可调用 `userdata` 方法绑定。此前只接受 `function`，可能把真实可用的计算音高接口误判为不可用。
- `voicing` 在当前宿主实际返回通道时开放，单次增量/曲线偏移上限为 0.3，最终值域仍由宿主定义约束。旧桥接不回退开放此参数。
- 声线三维默认设置按主组到当前组逐项合并，局部覆盖不再抹掉未覆盖的继承值。只公开 0～150 范围内的真实数值，缺项不补零。
- `voiceValues` 是只读默认设置，与 `vocalMode_Name` 自动化轨道分别处理。没有创建 `vocalMode_Name_pitch` 等未经文档支持的通道。
- MCP 新增 `get_parameter_catalog`，公开有界参数目录、声线来源和计算音高，不发送工程文件路径或完整参数指纹。先发现能力，再预览，用户确认后才能应用。
- MCP 不再注册录音、音频列表、比较、听评或返回音频内容的工具；历史资料保留在本机。

原生音高写入已存在于前版。本轮没有绕过既有选区、共享音符组、跨边界曲线、原生引导点、快照及回滚检查。原生曲线的输入是工程绝对 MIDI，转换时减去组移调；内部节点采用相对锚点坐标，预览由宿主实际插值读回。声线参数的“绘制模式”仍基于公开的 Automation 接口进行精简，不宣称存在声线专用的连续曲线对象。

## 开源实现参考

查阅了 [SVIXAGENT / AKDAgent](https://github.com/Akunda123/SVIXAGENT) 的音高、参数与宿主计算结果功能说明，以及 [SV-MCP](https://github.com/shotarokawade/SV-MCP) 的原生音高坐标、专用读取工具及预演流程。参考点是“先读取真实宿主能力、区分计算结果与编辑对象、写后读回”，不是工具数量或界面宣传。

这些项目不能作为 SynthV 官方 API 的替代文档，也没有据此宣称存在成熟、完整覆盖 2.3.0 的通用 SDK。本轮实现继续使用本项目既有 Python/Lua 桥接，未复制第三方源码、提示词或资源；不引入外部项目的任意脚本执行、声库文件编辑或自动应用机制。

## 验证边界

测试使用独立 Lua 虚拟机、模拟 API、临时资料及真实 stdio 协议握手，覆盖计算音高绑定、声线继承/缺项、参数限幅、MCP 退役入口和私有字段过滤。这些测试不构成所有声库的听感验收。更新桥接脚本并在 2.3.0 中重新运行后，需重新读取当前选区才能得到新目录和计算音高。

本机 2.3.0 已完成旧脚本备份、新脚本启动及只读选区检查：宿主返回原生音高能力、17 个参数通道（包含 `voicing` 和 8 个声线通道），计算音高状态为 `ready`，声线三维默认值可读取。此检查没有应用参数或保存用户工程，也不代表对所有声库、所有曲线边界的写入验证。
