<#
.SYNOPSIS
打包并安装 SynthV Assistant Lua 桥接。
.DESCRIPTION
只操作本助手的打包文件及目标同名 Lua 脚本。目标已存在时，先复制到本项目
数据目录下的 backups\bridge，再安装新文件；不会删除脚本目录、修改 SVP 工程、
启动 SynthV 或修改 Codex 全局配置。支持 -WhatIf 预览目标而不执行安装。
.EXAMPLE
.\scripts\install-bridge.ps1 -WhatIf
.EXAMPLE
.\scripts\install-bridge.ps1
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonExecutable = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw '未找到项目 .venv。请先按 README 准备本地 Python 环境。'
}
if ([string]::IsNullOrWhiteSpace($env:APPDATA)) {
    throw '未找到 APPDATA，无法确定 SynthV Studio 2 脚本目录。'
}

# 安装目标为已确认的 SynthV Studio 2 用户脚本目录，文件名固定；
# 不接收任意目标路径，避免脚本被误用于覆盖其他文件。
$scriptDirectory = Join-Path $env:APPDATA 'Dreamtonics\Synthesizer V Studio 2\scripts'
$targetScript = [System.IO.Path]::GetFullPath((Join-Path $scriptDirectory 'SynthVAssistant.lua'))
if (-not $PSCmdlet.ShouldProcess($targetScript, '打包桥接、备份现有同名文件并安装')) {
    return
}

Push-Location -LiteralPath $projectRoot
try {
    # 与 config.py 使用同一数据目录。相对路径按项目根目录解释，
    # 因为 Python 打包命令也从项目根目录运行。
    $dataDirectory = if ([string]::IsNullOrWhiteSpace($env:SYNTHV_ASSISTANT_DATA)) {
        Join-Path $projectRoot 'data'
    }
    elseif ([System.IO.Path]::IsPathRooted($env:SYNTHV_ASSISTANT_DATA)) {
        [System.IO.Path]::GetFullPath($env:SYNTHV_ASSISTANT_DATA)
    }
    else {
        [System.IO.Path]::GetFullPath((Join-Path $projectRoot $env:SYNTHV_ASSISTANT_DATA))
    }

    # 打包会把当前本机 IPC 路径写入 Lua；移动项目后必须重新执行本步骤。
    & $pythonExecutable -m synthv_assistant build-script
    if ($LASTEXITCODE -ne 0) {
        throw ('桥接打包失败，退出码为 {0}；未替换已安装文件。' -f $LASTEXITCODE)
    }
    $sourceScript = Join-Path $dataDirectory 'install\SynthVAssistant.lua'
    if (-not (Test-Path -LiteralPath $sourceScript -PathType Leaf)) {
        throw ('打包命令未生成预期文件：{0}' -f $sourceScript)
    }

    if (Test-Path -LiteralPath $targetScript) {
        if (-not (Test-Path -LiteralPath $targetScript -PathType Leaf)) {
            throw '安装目标存在但不是普通文件，未执行替换。'
        }
        $backupDirectory = Join-Path $dataDirectory 'backups\bridge'
        [System.IO.Directory]::CreateDirectory($backupDirectory) | Out-Null
        $backupName = 'SynthVAssistant-{0}-{1}.lua.bak' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), ([System.Guid]::NewGuid().ToString('N').Substring(0, 8))
        $backupPath = Join-Path $backupDirectory $backupName

        # 备份采用逐文件复制；只有备份成功后才继续覆盖同名目标。
        Copy-Item -LiteralPath $targetScript -Destination $backupPath -ErrorAction Stop
        Write-Host ('旧脚本已备份：{0}' -f $backupPath)
    }

    [System.IO.Directory]::CreateDirectory($scriptDirectory) | Out-Null
    Copy-Item -LiteralPath $sourceScript -Destination $targetScript -Force -ErrorAction Stop

    # 安装后比较文件摘要，避免复制不完整却向用户报告成功。
    $sourceHash = (Get-FileHash -LiteralPath $sourceScript -Algorithm SHA256).Hash
    $targetHash = (Get-FileHash -LiteralPath $targetScript -Algorithm SHA256).Hash
    if ($sourceHash -ne $targetHash) {
        throw '安装后文件校验失败，请保留备份并检查目标脚本；不要启动不完整文件。'
    }
    Write-Host ('桥接已安装：{0}' -f $targetScript)
    Write-Host '请在 SynthV 的 Scripts/脚本菜单重新扫描，并手动运行 SynthV Assistant。'
    Write-Host '若旧桥接仍在运行，请先结束旧会话或重启 SynthV；无需反复启动多个实例。'
}
finally {
    Pop-Location
}
