# 第三方组件与外部软件

`synthv/json.lua` 来自 [rxi/json.lua](https://github.com/rxi/json.lua)，采用 MIT 许可证。
版权声明为 `Copyright (c) 2020 rxi`。完整 MIT 许可文本保留在该文件开头；打包到 SynthV 的单文件脚本也保留它。本项目的 [MIT 许可证](LICENSE) 不替代该组件的原始版权声明。

Python 运行依赖 `numpy`、`mcp` 及可选测试依赖 `lupa` 通过包管理器安装，未将其发行包或源代码复制到仓库；准确版本范围见 [pyproject.toml](pyproject.toml)，各自许可证见安装包元数据。
原生进程回录实现依据 [Microsoft ApplicationLoopback 示例](https://github.com/microsoft/Windows-classic-samples/tree/main/Samples/ApplicationLoopback)
及 Windows COM 接口文档编写，没有打包微软示例的源文件。

MP3 转换调用用户自行安装的 FFmpeg；本仓库不附带 FFmpeg 二进制或源码，其许可取决于所安装的构建版本。.NET SDK/运行时同样由使用者自行安装。

Synthesizer V Studio、声库、Microsoft Windows 及外部模型服务分别由相应权利人提供，不包含在本项目中，也不因本项目采用 MIT 而获得新的使用或分发授权。产品名称只用于说明兼容性和接口用途，本项目不表示获得这些厂商的官方支持。
