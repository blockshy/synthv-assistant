<#
.SYNOPSIS
在前台启动 SynthV 本地调教工作台。
.DESCRIPTION
仅使用本项目已存在的 .venv；不会安装全局依赖、自动读取 .env、
启动 SynthV 或修改 AI 客户端配置。音频评审可在网页即时配置；
尚无本机保存配置时，兼容当前进程环境变量。
.PARAMETER Port
本机回环 HTTP 端口，默认 8765。端口占用时直接报告错误，不终止其他程序。
.EXAMPLE
.\scripts\start.ps1 -Port 8765
#>
[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# 从脚本位置计算项目根目录，不依赖调用者当前所在目录。
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonExecutable = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw '未找到项目 .venv。请按照 README 创建虚拟环境并执行 python -m pip install -e .。'
}

# Windows 进程回录有明确的系统版本要求；不满足条件时允许查看界面，
# 但清楚说明真实采集不可用，不自动改用麦克风或全系统录音。
if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw '此启动脚本面向 Windows；当前进程音频采集组件仅支持 Windows。'
}
if ([System.Environment]::OSVersion.Version.Build -lt 20348) {
    Write-Warning '当前 Windows build 低于 20348，按进程音频采集不可用；建议使用 Windows 11。'
}

# 只为环境后备设置默认 none；本机加密配置由服务读取，优先级更高。
if ([string]::IsNullOrWhiteSpace($env:SYNTHV_AUDIO_PROVIDER)) {
    $env:SYNTHV_AUDIO_PROVIDER = 'none'
}

Push-Location -LiteralPath $projectRoot
try {
    Write-Host ('工作台地址：http://127.0.0.1:{0}' -f $Port)
    Write-Host '请手动打开 SynthV 并运行 SynthV Assistant 脚本；按 Ctrl+C 停止本地服务。'
    Write-Host '点击网页“模型设置”即可即时配置；本启动脚本不读取 .env。'

    # 直接调用参数化的 Python 命令，不构造 shell 命令字符串。
    # 服务在当前终端前台运行，便于用户看到错误并主动停止。
    & $pythonExecutable -m synthv_assistant serve --port $Port
    if ($LASTEXITCODE -ne 0) {
        throw ('本地服务退出，退出码为 {0}。请查看上方错误。' -f $LASTEXITCODE)
    }
}
finally {
    # 即使服务失败，也恢复用户运行脚本前的工作目录。
    Pop-Location
}
