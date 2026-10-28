# SynthV Assistant 原生桌面工作台

这是面向 Windows 的 Flutter 客户端。界面由 Flutter 原生渲染，没有嵌入旧网页或 WebView；调教、音频和模型请求仍由项目现有 Python 核心执行。主要布局为会话导航、中央工作区和工程检查器，支持黑白浅色/深色主题，首次启动跟随系统。

完整使用流程、数据目录、架构与验收边界见 [桌面工作台说明](../docs/desktop-workbench.md)。项目的 SynthV 桥接要求和许可证见 [根目录 README](../README.md) 与 [MIT 许可证](../LICENSE)。

## 使用发行包

1. 解压完整发行包，保留 `synthv_desktop.exe` 同目录的资源、DLL 和 `backend` 等运行文件。
2. 双击 `synthv_desktop.exe`。客户端会启动自己的本机后台，无需另外安装 Python 或 .NET 运行时，也不用先启动旧网页服务。
3. 打开“连接管理”检查脚本和目录；已有脚本在线时可直接连接，缺少脚本时可备份并安装。具体步骤见 [首次连接 SynthV](../docs/desktop-workbench.md#首次连接-synthv)；纯文字咨询和素材管理不要求先开启工程修改权限。
4. 在“模型设置”中保存平台、模型、地址和 API 密钥。保存不会调用模型；新会话默认平台需显式选择。

发行包默认使用 `%LOCALAPPDATA%\SynthV Assistant\data` 保存会话、素材及加密设置。源码开发默认沿用仓库 `data/`；可通过 `SYNTHV_ASSISTANT_DATA` 指定同一个已有目录。切换目录不会自动搬运数据，桥接也必须重新绑定对应目录。

音频导入支持 WAV/MP3。MP3 解码额外要求 PATH 中有可用的 FFmpeg；没有 FFmpeg 时可导入 PCM WAV。音频导入到素材库、附加到草稿和真正发送给云端模型是三个不同动作。

## 从源码开发

开发机器需要 Windows、项目 `.venv`、Flutter Windows 工具链和 Visual Studio 的 C++ 桌面开发组件。当前客户端使用 Dart `^3.13.5`，本轮采用 Flutter `3.47.6`。构建进程音频采集器和完整发行包还需要 .NET SDK 10；这是构建机依赖，不是发行包用户依赖。

在仓库根目录准备 Python 环境：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

使用仓库入口启动：

```powershell
.\scripts\start-desktop.ps1
```

Flutter 不在 PATH 时，可传 `-FlutterPath 'D:\Tools\Flutter\bin\flutter.bat'`；`-DataDirectory` 可指定独立资料目录，`-Release` 可在开发入口采用 release 模式。启动脚本只使用已有环境，不安装依赖或自动构建录音采集器。

也可以从 `desktop/` 直接运行 Flutter；此方式会查找项目的 `.venv\Scripts\python.exe`：

```powershell
flutter pub get
flutter run -d windows
```

直接运行 Flutter 不会自动构建音频采集器。首次开发录音功能前，在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -m synthv_assistant build-capture
```

不要同时运行指向同一数据目录的旧网页和桌面工作台。开发时采用独立测试目录，可避免把测试会话和设置混入日常资料：

```powershell
$env:SYNTHV_ASSISTANT_DATA = Join-Path $env:TEMP 'synthv-assistant-development'
.\scripts\start-desktop.ps1
```

这个示例在仓库根目录执行，仅影响该 PowerShell 进程及其子进程；不会复制原有会话、凭据或工程。

## 检查与打包

在 `desktop/` 执行：

```powershell
flutter analyze
flutter test
flutter build windows --release
```

单独的 Flutter release 构建只生成前端。准备完整发行包时，在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\scripts\package-desktop.ps1
```

打包入口同样支持 `-FlutterPath`。结果保存为 `dist/SynthVAssistant-Windows-时间戳/`、同名 ZIP 和 SHA-256 校验文件；发布应使用完整 ZIP。

打包会组合 Flutter release、冻结后的 Python 后台和自包含 Windows 音频采集器。不得手动把整个仓库或用户 `data/` 复制进发行目录；资料、API 密钥、录音、日志和私人工程都不属于发布内容。

发布脚本把 Dart AOT 调试符号单独保存到本机 `build/desktop-symbols-*`，不随 ZIP 发布；采集器不生成 PDB，音频插件也不包含 WIL 的源码路径诊断。Flutter 仍可能保留运行时插件注册所需的构建 URI，因此应使用不含个人姓名或私有工程名的构建目录，不能把符号分离视为完整的隐私扫描。

## 开发约定

- 共享颜色和尺寸在 `lib/theme.dart` 维护；管理按钮使用图标及中文 Tooltip，主要操作保留文字。
- 页面只通过 `lib/core/api.dart` 调用受控本机接口，不绕过参数校验、预览票据和确认流程。
- 会话请求只提交一次，轮询已有任务；不自动重发模型请求，不自动接受提案。
- 所有业务代码保留具体、规范的中文注释，测试使用模拟 API 与隔离资料。
- 原生音频预听按用户点击才加载；不将认证令牌拼进媒体 URL。

`test/pages_test.dart` 覆盖素材固定操作栏、附件次序/字段白名单、删除确认、失败后密钥清空和读取选区失败后的录音拦截。其他测试覆盖工作台布局、会话状态和曲线显示。自动测试不证明云模型效果、特定声库调教质量或真实工程写入正确，仍需按说明进行真机验收。
