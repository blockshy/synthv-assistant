<#
.SYNOPSIS
启动原生 Flutter 工作台，默认复用当前项目的数据目录。
.DESCRIPTION
开发入口只启动本机已有环境，不安装依赖、不联网调用模型、不启动网页。
可以用 -FlutterPath 指定 flutter.bat；-DataDirectory 可选择独立数据目录。
切换之前请先退出使用同一数据目录的网页服务和旧工作台。
#>
param(
    [string]$FlutterPath = 'flutter',
    [string]$DataDirectory = '',
    [switch]$Release
)
$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$desktopRoot = Join-Path $projectRoot 'desktop'
$pythonExecutable = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw '请先按 README 准备项目 .venv；该入口不是免安装发行包。'
}
if (-not (Get-Command $FlutterPath -ErrorAction SilentlyContinue)) {
    throw '未找到 Flutter，请把 SDK 加入 PATH 或使用 -FlutterPath 指定 flutter.bat。'
}
# 环境覆盖仅影响本次启动的子进程，退出后恢复调用者原有环境。
$previousData = $env:SYNTHV_ASSISTANT_DATA
try {
    $env:SYNTHV_ASSISTANT_DATA = if ($DataDirectory) {
        [System.IO.Path]::GetFullPath($DataDirectory)
    } elseif ($previousData) { $previousData } else { Join-Path $projectRoot 'data' }
    Push-Location -LiteralPath $desktopRoot
    try {
        $runMode = if ($Release) { '--release' } else { '--debug' }
        & $FlutterPath run -d windows $runMode
        if ($LASTEXITCODE -ne 0) { throw '桌面工作台运行失败，请检查上方 Flutter 输出。' }
    } finally { Pop-Location }
} finally { $env:SYNTHV_ASSISTANT_DATA = $previousData }
