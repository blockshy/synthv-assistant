# SynthV 对话工作台

面向 **Synthesizer V Studio 2 Pro 独立版 / Windows** 的本地调教助手。使用 Flutter 原生工作台、Python 本机服务、官方 Lua 脚本及 stdio MCP，帮助读取工程、分析选区、编辑参数并生成可确认的预览。默认只读；AI 建议不自动写入工程。本项目与 Dreamtonics 没有官方关联，不包含 SynthV 软件、声库或歌曲。

**0.4.0** 对照 SynthV **2.3.0** 的官方更新与公开脚本文档完善桥接和工程检查器，修复设置页往返出现白块的状态冲突，统一下拉及展开控件。版本与 API 支持边界见 [2.3.0 支持记录](docs/synthv-230-support.md)。

本版移除全部音频操作入口：不再导入、录制、播放或向模型发送音频，也不再提供 A/B 听评 MCP 工具。已有录音、上传文件、附件元数据及历史会话原样保留，升级不自动清理数据。模型依据文字、乐谱、当前参数和宿主计算音高提供建议；实际听感由用户在 SynthV 中试听确认。

## 启动与升级

完整 Windows 发行包解压后双击 `synthv_desktop.exe`，保留同目录 DLL、`data` 静态资源和 `backend`。无需另装 Python 或 .NET；系统需要 Microsoft Visual C++ x64 运行库。使用与数据迁移见 [桌面工作台](docs/desktop-workbench.md)，开发和打包见 [desktop/README.md](desktop/README.md)。

- 发行包业务数据默认位于 `%LOCALAPPDATA%\SynthV Assistant\data`；源码运行默认使用仓库 `data/`。
- `SYNTHV_ASSISTANT_DATA` 可指定已有资料目录；不会自动搬移、合并或删除旧资料。
- 更新前结束任务并关闭旧程序；不要让旧网页和新桌面同时管理同一数据目录。
- 本次桥接有更新。打开 **SynthV 连接 → 安装匹配脚本**，备份安装后在 SynthV 结束旧脚本、重新扫描并运行 **SynthV Assistant**。安装文件不等于运行中的桥接已经更新。
- 桥接在线却无法读取时，检查当前目录、心跳与脚本版本。不要为诊断先开启写入权限。

## 工作台流程

1. 在 **模型服务** 添加 OpenAI/兼容服务、Qwen 或 Gemini 平台，保存地址与密钥。可先不填默认模型，再获取目录或手动填写模型 ID。
2. 按需设为 **新会话默认平台**；已有会话保留选择。会话内用同一选择器确定平台与模型，再选择推理强度与曲线模式。模型目录先读本机缓存，明确刷新才联网。
3. 在 SynthV 中选中目标音符；工程检查器可以分别读取工程和选区，查看实际音符、参数现值、声线目录及支持状态。
4. 输入文字目标，按需勾选 **附带选区**。发送后即时显示消息、处理阶段及供应商实际提供的思考摘要。
5. 先生成单项或组合预览，检查参数、范围和曲线；用户确认后才允许应用。写后校验失败或结果未知时不自动重试。
6. 在 SynthV 中试听。完整恢复到原状态后，可重新预览并确认应用；旧票据不能直接反复重放。

“模型服务”与“SynthV 连接”分别管理云端模型和本机宿主。设置页使用平台列表、连接配置、模型目录和固定保存栏；跨页、切换平台或重新加载前保护未保存修改。设计记录见 [Agent 工作台交互](docs/agent-workbench-design.md)。

## 参数与曲线

| 功能 | 当前范围 |
| --- | --- |
| 工程读取 | 工程、轨道、音符组、宿主版本、播放位置和当前选区；实际可用字段由宿主返回 |
| 通用参数 | 气声、张力、响度、性别、音高偏移、音色偏移、颤音包络、浊音/清音；必须由本次目录确认可用 |
| 声库声线 | 宿主已返回或用户核对的 `vocalMode_*` 列表、来源及默认设置；不虚构声库完整枚举 |
| 自动化曲线 | 整体增量、2–64 个稀疏控制点、手绘与控制点编辑；模型可使用逐音符局部包络 |
| 原生音高 | 官方 `PitchControlCurve` 对象，绝对 MIDI 半音；与音分单位的 `pitchDelta` 区分 |
| 检查器 | 工程和选区分开读取、结构化音符列表、参数现值、不可用原因、草稿重置/撤销、固定预览与确认栏 |
| 预览与复用 | 多参数合图，音符与不同单位的参数分轨；旧方案可重新映射到当前选区后预览 |

计算音高是宿主返回的有限采样，只作只读参考，不是录音实测，也不会自动转换为原生曲线写回。声线默认的 `pitch/timbre/pronunciation` 三维设置与 `vocalMode_*` 自动化强度不是同一种数据，本版显示前者但不把它冒充可绘制通道。

参数影响选中音符覆盖的**连续时间段**，包括中间休止和未选中音符。当前最多 128 个音符、30 秒；共享组、越界、复杂边界或不可验证的原生曲线会被拒绝。模型只提案，不自动改变音符乐谱、组移调或选区。不直接改写 `.svp`，不开放任意脚本、音符组克隆或通用工程事务。详细边界见 [参数曲线](docs/parameter-curves.md) 与 [生成逻辑](docs/tuning-quality.md)。

## 从源码启动

需要 Windows、Python 3.10+、SynthV Studio 2 Pro 和可用声库；Flutter Windows 构建还需要 Flutter 与 Visual Studio C++ 桌面工具链。音频退役后不再要求 .NET 采集器或 FFmpeg。

```powershell
# 创建项目独立环境；以下命令从仓库根目录执行。
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"

# 安装固定名称的桥接；已有同名脚本先备份，不修改工程。
.\scripts\install-bridge.ps1

# 启动 Flutter 原生工作台，可用 -FlutterPath 指定 flutter.bat。
.\scripts\start-desktop.ps1
```

在 SynthV 中重新扫描并运行桥接，停止播放后选取一小段音符。移动源码或改变资料目录后，应在“SynthV 连接”连接已知脚本，或重新安装与目录匹配的脚本。

旧网页源码仅保留用于兼容与历史参考；本版新增交互以 Flutter 为准。旧客户端调用上传、录音、比较和听评接口会收到退役提示，带音频附件的消息在读取文件或排队前拒绝。旧资料的受控读取保留，不等于重新启用音频操作。

## MCP 接入

MCP 客户端需要支持启动本地 stdio 进程。配置示例见 [mcp-config.example.json](scripts/mcp-config.example.json)，将占位路径改为自己的路径；程序不会自动修改客户端全局设置。

```text
command: <项目绝对路径>\.venv\Scripts\python.exe
args:    -m synthv_assistant mcp
```

MCP 与桌面应使用一致的业务目录和桥接目标。常用工具：

- 发现：`status`、`get_project`、`get_selection`、`get_parameter_catalog`。
- 参数：`preview_parameter`、`preview_curve`；确认后使用 `set_write_mode`、`apply_preview`，恢复使用 `restore_last_edit`。
- 任务：`get_job`，只查询创建它的同一服务进程。

示例：“读取当前选区和可用声线，给出张力小幅变化的预览，等我确认后再应用。”客户端的工具审批应要求用户确认具体修改；已开启写入权限不代表后续建议自动获批。跨进程操作锁保证宿主命令串行，遇到忙碌或未知结果应先查询，不能反复提交。

## 数据、隐私与验证

| 内容 | 保存位置 |
| --- | --- |
| 会话与提案 | `conversations/`，单会话最多 100 条消息、2 MB |
| 桥接 IPC、安装及备份 | `ipc/`、`install/`、`backups/` |
| 模型设置与缓存 | `audio-settings.json`（保留历史文件名的内置平台配置）、`model-platforms.json`、`model-catalogs/` |
| 旧音频及管理记录 | `recordings/`、`uploads/`、`metadata/`；升级保留，不再新增音频 |

云端密钥由当前 Windows 账户 DPAPI 加密，页面不回填。改变地址或协议需要重新填写密钥。保存配置不调用模型；测试连接只访问模型目录，不代表推理能力或账户额度已经验证。新会话只发送本次文字、必要历史文字与明确附带的安全选区摘要，不发送工程路径或音频。历史附件名称仅在本地记录展示。

```powershell
# 模拟供应商、Lua 宿主与隔离资料的自动化回归，不调用真实付费模型。
.\.venv\Scripts\python.exe -m unittest discover -s tests
# 在 desktop 目录执行 Flutter 检查。
flutter analyze
flutter test
```

模拟测试和界面验收不能证明实际音质改善。参数最终效果必须由用户在真实声库中试听；不同宿主版本和声库仍可能有各自限制。私有数据、密钥、日志、录音、截图及发行构建目录不进入源码仓库。

## 许可证

本项目采用 [MIT](LICENSE)。第三方组件各自遵守原许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。外部项目仅用于研究公开接口与通用交互，不复制其代码、品牌或素材；本项目许可不授予 SynthV 软件和声库的分发权。
