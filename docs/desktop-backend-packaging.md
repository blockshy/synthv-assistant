# 桌面后台打包与许可资料

`scripts/package-desktop-backend.ps1` 使用项目 `.venv` 中的 PyInstaller 6 系列构建 one-dir 后台。音频功能退役后，脚本不再构建或携带 `ProcessAudioCapture` 和自包含 .NET 运行时，也不要求 .NET SDK。开发环境需事先安装打包工具；脚本不自动安装全局运行时或下载应用用户数据。产物为 `dist/backend/synthv-backend/`，桌面发行包应将整个目录放在 Flutter 程序旁的 `backend/`，不能只复制 exe。

构建只显式加入 `synthv/` Lua 资源，不加入 `data/` 或旧采集器目录。历史 Python 模块暂不做大范围依赖裁剪；这不表示其音频入口仍然开放。桌面后台从 stdout 交付随机端口及令牌，所有 API 均需令牌；父进程保持 stdin 管道，关闭时调用认证 shutdown 接口或等待在途任务排空。

## 第三方许可收集

打包最后运行 `scripts/collect_backend_licenses.py`，在后台目录生成 `licenses/`：

| 内容 | 来源 |
|---|---|
| `python-runtime/LICENSE.txt` | 与构建解释器一致的 Python 安装目录随附许可，保留原始字节 |
| `dotnet-runtime/LICENSE.TXT`、`THIRD-PARTY-NOTICES.TXT` | 仅复核确实含旧采集器的产物时生成：根据实际 `runtimeconfig.json` 确认版本，再从同版本 NuGet 包读取；当前新发行包不包含此项 |
| `python-packages/` | 将 PyInstaller `Analysis-00.toc` 实际输入路径与安装包的 wheel RECORD 匹配，复制命名为 license、copying、notice、copyright 的随附文本；另外保留已进入 exe 的 PyInstaller 启动器许可 |
| `manifest.json` | Python 版本、分发包名称与版本、许可相对路径和收集方法；只有产物实际含 .NET 时才记录其版本与来源，不输出开发机器绝对路径 |

收集脚本只读取构建清单、依赖元数据和相应许可文本，不执行清单、不导出 NuGet 配置或 Python METADATA，也不复制用户资料。没有找到已打包 Python 依赖的许可正文，或者审计旧包时找不到与实际 .NET 版本一致的声明，脚本都会报错，不把许可证名称当作完整授权文本。若当前产物不含采集器但保留了旧 `dotnet-runtime/` 声明，脚本要求重新构建后台；不会默默沿用或递归删除来源不明的目录。可单独执行以下命令为已构建包补充相同资料，无需重新编译二进制：

```powershell
.\.venv\Scripts\python.exe scripts/collect_backend_licenses.py
```

`licenses/` 是发行包的一部分，不能在压缩、安装或更新时省略。项目自己的 MIT 许可证不改变这些组件的授权条件。上述流程收集可核对的原始资料，**不保证已自动判断或履行所有版权、通知、源码提供、商标和再分发义务**；正式发行与依赖升级后仍应按实际内容复核。Flutter、桌面插件及最外层项目的许可证由桌面发行脚本另外包含。
