# 桌面后台打包与许可资料

`scripts/package-desktop-backend.ps1` 使用项目 `.venv` 中的 PyInstaller 6 系列构建 one-dir 后台，并通过 .NET SDK 发布 `win-x64` 自包含采集器。开发环境需事先安装打包工具；脚本不自动安装全局运行时或下载应用用户数据。产物为 `dist/backend/synthv-backend/`，桌面发行包应将整个目录放在 Flutter 程序旁的 `backend/`，不能只复制 exe。

构建只显式加入 `synthv/` Lua 资源和采集器发布产物，不加入 `data/`。桌面后台从 stdout 交付随机端口及令牌，所有 API 和音频均需令牌；父进程保持 stdin 管道，关闭时调用认证 shutdown 接口或等待在途任务排空。

## 第三方许可收集

打包最后运行 `scripts/collect_backend_licenses.py`，在后台目录生成 `licenses/`：

| 内容 | 来源 |
|---|---|
| `python-runtime/LICENSE.txt` | 与构建解释器一致的 Python 安装目录随附许可，保留原始字节 |
| `dotnet-runtime/LICENSE.TXT`、`THIRD-PARTY-NOTICES.TXT` | 根据采集器实际 `runtimeconfig.json` 确认版本，再从同版本 `Microsoft.NETCore.App.Runtime.win-x64` NuGet 包读取 |
| `python-packages/` | 将 PyInstaller `Analysis-00.toc` 实际输入路径与安装包的 wheel RECORD 匹配，复制命名为 license、copying、notice、copyright 的随附文本；另外保留已进入 exe 的 PyInstaller 启动器许可 |
| `manifest.json` | Python/.NET 版本、分发包名称与版本、许可相对路径和收集方法；不输出开发机器绝对路径 |

收集脚本只读取构建清单、依赖元数据和相应许可文本，不执行清单、不导出 NuGet 配置或 Python METADATA，也不复制用户资料。没有找到已打包 Python 依赖的许可正文，或者找不到与 .NET 产物版本一致的声明时，脚本报错，不把许可证名称当作完整授权文本。可单独执行以下命令为已构建包补充相同资料，无需重新编译二进制：

```powershell
.\.venv\Scripts\python.exe scripts/collect_backend_licenses.py
```

`licenses/` 是发行包的一部分，不能在压缩、安装或更新时省略。项目自己的 MIT 许可证不改变这些组件的授权条件。上述流程收集可核对的原始资料，**不保证已自动判断或履行所有版权、通知、源码提供、商标和再分发义务**；正式发行与依赖升级后仍应按实际内容复核。Flutter、桌面插件及最外层项目的许可证由桌面发行脚本另外包含。
