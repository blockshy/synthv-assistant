<#
.SYNOPSIS
使用桌面发行包生成并安装 SynthV Assistant Lua 桥接。
.DESCRIPTION
只使用发行包内的 backend/synthv-backend.exe，不依赖 Python 或 .NET 安装。
优先采用 DataDirectory 参数，其次 SYNTHV_ASSISTANT_DATA，最后采用桌面
发行包默认的 LOCALAPPDATA/SynthV Assistant/data。原有同名脚本先备份，
本脚本不启动 SynthV、不运行桥接、不读取或修改 SVP 工程。
.PARAMETER DataDirectory
可选的业务数据目录。使用自定义目录时，启动桌面程序也必须设置相同的
SYNTHV_ASSISTANT_DATA；建议使用绝对路径，避免启动目录造成歧义。
.EXAMPLE
.\scripts\install-desktop-bridge.ps1 -WhatIf
.EXAMPLE
.\scripts\install-desktop-bridge.ps1
.EXAMPLE
.\scripts\install-desktop-bridge.ps1 -DataDirectory 'D:\SynthVAssistantData'
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$DataDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$packageRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$backendExecutable = Join-Path $packageRoot 'backend\synthv-backend.exe'
if (-not (Test-Path -LiteralPath $backendExecutable -PathType Leaf)) {
    throw '未找到桌面发行包后台。请在完整发行包中运行此脚本；源码安装使用 install-bridge.ps1。'
}
if ([string]::IsNullOrWhiteSpace($env:APPDATA)) {
    throw '未找到 APPDATA，无法确定 SynthV Studio 2 脚本目录。'
}

# 与桌面发行包选择同一默认目录；显式参数只影响本次生成，不能偷偷更改
# 用户永久环境变量。随后会打印实际目录，便于与桌面启动配置核对。
if ([string]::IsNullOrWhiteSpace($DataDirectory)) {
    $DataDirectory = $env:SYNTHV_ASSISTANT_DATA
}
if ([string]::IsNullOrWhiteSpace($DataDirectory)) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw '未找到 LOCALAPPDATA，请使用 -DataDirectory 指定资料目录。'
    }
    $DataDirectory = Join-Path $env:LOCALAPPDATA 'SynthV Assistant\data'
}
$resolvedData = [System.IO.Path]::GetFullPath($DataDirectory)
$scriptDirectory = Join-Path $env:APPDATA 'Dreamtonics\Synthesizer V Studio 2\scripts'
$targetScript = [System.IO.Path]::GetFullPath((Join-Path $scriptDirectory 'SynthVAssistant.lua'))
$sourceScript = Join-Path $resolvedData 'install\SynthVAssistant.lua'
if (-not $PSCmdlet.ShouldProcess($targetScript, ('使用资料目录 {0} 生成桥接、备份原脚本并安装' -f $resolvedData))) {
    return
}

# build-script 是通用 CLI 子命令，因此必须显式设置数据目录。保存并恢复
# 当前进程原值，避免这次安装改变调用者后续启动其他资料目录的行为。
$previousData = $env:SYNTHV_ASSISTANT_DATA
try {
    $env:SYNTHV_ASSISTANT_DATA = $resolvedData
    & $backendExecutable build-script
    if ($LASTEXITCODE -ne 0) {
        throw ('桥接生成失败，退出码为 {0}；尚未替换 SynthV 中的脚本。' -f $LASTEXITCODE)
    }
    if (-not (Test-Path -LiteralPath $sourceScript -PathType Leaf)) {
        throw '后台未生成预期的桥接脚本，尚未修改 SynthV 脚本目录。'
    }

    if (Test-Path -LiteralPath $targetScript) {
        if (-not (Test-Path -LiteralPath $targetScript -PathType Leaf)) {
            throw '安装目标存在但不是普通文件，未执行替换。'
        }
        $backupDirectory = Join-Path $resolvedData 'backups\bridge'
        [System.IO.Directory]::CreateDirectory($backupDirectory) | Out-Null
        $backupName = 'SynthVAssistant-{0}-{1}.lua.bak' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), ([System.Guid]::NewGuid().ToString('N').Substring(0, 8))
        Copy-Item -LiteralPath $targetScript -Destination (Join-Path $backupDirectory $backupName) -ErrorAction Stop
        Write-Host ('原脚本已备份至：{0}' -f (Join-Path $backupDirectory $backupName))
    }

    [System.IO.Directory]::CreateDirectory($scriptDirectory) | Out-Null
    Copy-Item -LiteralPath $sourceScript -Destination $targetScript -Force -ErrorAction Stop
    if ((Get-FileHash -LiteralPath $sourceScript -Algorithm SHA256).Hash -ne
        (Get-FileHash -LiteralPath $targetScript -Algorithm SHA256).Hash) {
        throw '安装后摘要校验失败，请保留备份并检查脚本文件，不要启动不完整桥接。'
    }
    Write-Host ('桌面资料目录：{0}' -f $resolvedData)
    Write-Host ('桥接已安装：{0}' -f $targetScript)
    Write-Host '请在 SynthV 的脚本菜单重新扫描，并手动运行 SynthV Assistant。'
    Write-Host '若旧桥接仍在运行，请先结束旧桥接或重启 SynthV；本脚本不会自动运行宿主。'
    if ($PSBoundParameters.ContainsKey('DataDirectory')) {
        Write-Host '本次指定了自定义资料目录；启动桌面程序时请设置相同的 SYNTHV_ASSISTANT_DATA。'
    }
}
finally {
    $env:SYNTHV_ASSISTANT_DATA = $previousData
}
