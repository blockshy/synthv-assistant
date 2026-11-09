<#
.SYNOPSIS
构建 Flutter 安装包使用的自包含 Python 后台。
.DESCRIPTION
仅收集明确列出的源码、Lua 资源及编译产物，不读取或打包 data、密钥、
会话、录音和工程。输出到仓库 dist/backend，Flutter 打包脚本可整体复制。
开发机器需事先在本项目 .venv 安装 PyInstaller；不再构建音频采集器或要求 .NET SDK。
#>
param()
$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$backendDist = [System.IO.Path]::GetFullPath((Join-Path $projectRoot 'dist\backend'))
$backendTarget = [System.IO.Path]::GetFullPath((Join-Path $backendDist 'synthv-backend'))
# PyInstaller --noconfirm 会替换自身的旧输出，先核对最终绝对目标位于项目
# 的专属构建目录内；不能把用户提供的目录直接交给自动清理步骤。
if (-not $backendTarget.StartsWith($projectRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw '后台构建输出不在预期项目目录内，已停止。'
}
$pythonExecutable = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw '缺少项目 .venv，请先完成开发环境安装。'
}
& $pythonExecutable -m PyInstaller --version
if ($LASTEXITCODE -ne 0) {
    throw '请先在项目 .venv 安装 PyInstaller：.\.venv\Scripts\python.exe -m pip install pyinstaller'
}
# 音频入口退役后不再构建或收集 ProcessAudioCapture，也不携带其 .NET 运行时。
# 历史 Python 模块仍可随核心保留，但发布资源只显式加入 SynthV Lua 脚本。
# one-dir 保留透明资源布局；禁止使用仓库根目录或 data 的递归收集规则。
& $pythonExecutable -m PyInstaller --noconfirm --onedir --console --name synthv-backend `
    --distpath $backendDist `
    --workpath (Join-Path $projectRoot 'build\desktop-backend') `
    --specpath (Join-Path $projectRoot 'build') `
    --paths $projectRoot --collect-submodules synthv_assistant `
    --add-data ((Join-Path $projectRoot 'synthv') + ';synthv') `
    (Join-Path $PSScriptRoot 'desktop_backend_entry.py')
if ($LASTEXITCODE -ne 0) { throw '桌面后台打包失败。' }
& $pythonExecutable (Join-Path $PSScriptRoot 'collect_backend_licenses.py')
if ($LASTEXITCODE -ne 0) { throw '后台许可资料收集失败，请完成依赖许可核对后再发行。' }
Write-Output ('桌面后台已生成：' + (Join-Path $projectRoot 'dist\backend\synthv-backend\synthv-backend.exe'))
