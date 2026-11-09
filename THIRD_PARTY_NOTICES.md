# 第三方组件与外部软件

`synthv/json.lua` 来自 [rxi/json.lua](https://github.com/rxi/json.lua)，采用 MIT 许可证。
版权声明为 `Copyright (c) 2020 rxi`。完整 MIT 许可文本保留在该文件开头；打包到 SynthV 的单文件脚本也保留它。本项目的 [MIT 许可证](LICENSE) 不替代该组件的原始版权声明。

Python 运行依赖 `numpy`、`mcp` 及可选测试依赖 `lupa` 通过包管理器安装；源码仓库不包含其发行包。准确版本范围见 [pyproject.toml](pyproject.toml)。桌面发行包包含冻结 Python 后台，原始运行时与依赖许可位于包内 `backend/licenses/`；收集方式及边界见 [后台打包说明](docs/desktop-backend-packaging.md)。
历史原生进程回录源码依据 [Microsoft ApplicationLoopback 示例](https://github.com/microsoft/Windows-classic-samples/tree/main/Samples/ApplicationLoopback)
及 Windows COM 接口文档编写，没有打包微软示例的源文件。音频功能退役后，当前后台发行包不再构建或携带 `ProcessAudioCapture` 及其自包含 .NET 运行时。

历史 MP3 转换代码调用用户自行安装的 FFmpeg；本仓库及桌面发行包不附带 FFmpeg，其许可取决于所安装的构建版本。当前工作台不再提供音频导入、录音、试听或听评入口，正常构建也不要求 .NET SDK。许可收集器只在审计确实包含旧采集器的产物时加入其运行时声明，不为当前发行包虚报 .NET 依赖。

Flutter Windows 前端使用 Flutter/Dart、audioplayers、file_selector、flutter_markdown_plus、shared_preferences、window_manager、screen_retriever 等组件，版本由 `desktop/pubspec.lock` 固定。Flutter 构建生成的依赖许可保留在 `data/flutter_assets/NOTICES.Z`，打包脚本另导出 `FLUTTER_DEPENDENCY_LICENSES.txt` 便于查阅。项目 MIT 许可不替代这些依赖的许可证。Windows 应用声波图标由本项目代码独立生成；没有复制其他 AI 客户端的界面源代码或品牌素材。

音频入口退役与依赖删减分开进行：当前锁定清单仍保留部分历史 Flutter/Python 依赖。只要组件实际进入发行包，其原始许可仍须随包保留，不能因界面已隐藏就省略。

`audioplayers_windows` 的原生构建还使用 Microsoft Windows Implementation Library（WIL）。该依赖由插件通过 NuGet 获取，不包含在 Dart 自动汇总的许可证中；发行包在 `licenses/windows-implementation-library/` 单独保留本次构建版本的 `LICENSE` 和 `ThirdPartyNotices.txt` 原文。

Synthesizer V Studio、声库、Microsoft Windows 及外部模型服务分别由相应权利人提供，不包含在本项目中，也不因本项目采用 MIT 而获得新的使用或分发授权。产品名称只用于说明兼容性和接口用途，本项目不表示获得这些厂商的官方支持。
