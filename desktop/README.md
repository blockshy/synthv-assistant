# SynthV Assistant 原生桌面工作台

这是面向 Windows 的 Flutter 客户端。界面由 Flutter 原生渲染，没有嵌入旧网页或 WebView；调教与模型请求由项目现有 Python 核心执行。主要布局为会话导航、中央工作区和工程检查器，支持黑白浅色/深色主题，首次启动跟随系统。

完整使用流程、数据目录、架构与验收边界见 [桌面工作台说明](../docs/desktop-workbench.md)。项目的 SynthV 桥接要求和许可证见 [根目录 README](../README.md) 与 [MIT 许可证](../LICENSE)。

## 使用发行包

1. 解压完整发行包，保留 `synthv_desktop.exe` 同目录的资源、DLL 和 `backend` 等运行文件。
2. 双击 `synthv_desktop.exe`。客户端会启动自己的本机后台，无需另外安装 Python，也不用先启动旧网页服务；新版不包含音频采集器和 .NET 运行时。
3. 打开“SynthV 连接”检查脚本和目录；已有脚本在线时可直接连接，缺少脚本时可备份并安装。具体步骤见 [首次连接 SynthV](../docs/desktop-workbench.md#首次连接-synthv)；纯文字咨询和读取选区不要求先开启工程修改权限。
4. 在“模型服务”中先保存平台地址和 API 密钥，再获取或填写模型。保存不会调用模型；新会话默认平台需显式选择。

发行包默认使用 `%LOCALAPPDATA%\SynthV Assistant\data` 保存会话、工程缓存及加密设置。源码开发默认沿用仓库 `data/`；可通过 `SYNTHV_ASSISTANT_DATA` 指定同一个已有目录。切换目录不会自动搬运数据，桥接也必须重新绑定对应目录。

当前工作台已退役音频导入、素材、录音、试听、音频附件和 AI 听评入口。已有本地音频文件和历史会话资料不会自动删除，也不会在模型请求中重新发送；当前使用流程不需要 FFmpeg。

## 从源码开发

开发机器需要 Windows、项目 `.venv`、Flutter Windows 工具链和 Visual Studio 的 C++ 桌面开发组件。当前客户端使用 Dart `^3.13.5`，验证使用 Flutter `3.47.6`。完整发行包不再构建进程音频采集器，因此不要求 .NET SDK。

在仓库根目录准备 Python 环境：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

使用仓库入口启动：

```powershell
.\scripts\start-desktop.ps1
```

Flutter 不在 PATH 时，可传 `-FlutterPath 'D:\Tools\Flutter\bin\flutter.bat'`；`-DataDirectory` 可指定独立资料目录，`-Release` 可在开发入口采用 release 模式。启动脚本只使用已有环境，不自动安装依赖。

也可以从 `desktop/` 直接运行 Flutter；此方式会查找项目的 `.venv\Scripts\python.exe`：

```powershell
flutter pub get
flutter run -d windows
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

打包会组合 Flutter release、冻结后的 Python 后台及 SynthV Lua 桥接资源，不再加入 `ProcessAudioCapture` 或 .NET 运行时。不得手动把整个仓库或用户 `data/` 复制进发行目录；资料、API 密钥、历史录音、日志和私人工程都不属于发布内容。

发布脚本把 Dart AOT 调试符号单独保存到本机 `build/desktop-symbols-*`，不随 ZIP 发布。Flutter 仍可能保留运行时插件注册所需的构建 URI，因此应使用不含个人姓名或私有工程名的构建目录，不能把符号分离视为完整的隐私扫描。历史依赖实际进入产物时，仍须保留其原始许可证。

## 开发约定

- 共享颜色和尺寸在 `lib/theme.dart` 维护；管理按钮使用图标及中文 Tooltip，主要操作保留文字。
- 下拉框使用 `WorkbenchDropdown`；展开区使用 `WorkbenchExpansionTile`，为各条目提供独立 `storageId`，避免滚动位置和展开状态冲突。
- 页面只通过 `lib/core/api.dart` 调用受控本机接口，不绕过参数校验、预览票据和确认流程。
- 会话请求只提交一次，轮询已有任务；不自动重发模型请求，不自动接受提案。
- 所有业务代码保留具体、规范的中文注释，测试使用模拟 API 与隔离资料。
- 已退役音频入口不得通过隐藏控件、旧 HTTP 路由或 MCP 工具重新触发；历史资料按兼容数据保留。

回归覆盖平台草稿保护、统一控件尺寸、切页状态恢复、会话状态、音频退役边界和曲线显示。自动测试不证明云模型效果、特定声库调教质量或真实工程写入正确，仍需按说明进行真机验收。
