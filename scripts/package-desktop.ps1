<#
.SYNOPSIS
构建可直接解压运行的 Windows x64 原生工作台。
.DESCRIPTION
只复制 Flutter release、冻结后台和明确列出的公开说明。每次创建全新目录，
不把仓库 data、开发环境、日志、工程、录音或密钥带入发行包。
-SkipBackend 仅用于后台源码未改动且已完成对应构建的本机构建循环。
#>
param(
    [string]$FlutterPath = 'flutter',
    [switch]$SkipBackend
)
$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$pythonExecutable = Join-Path $projectRoot '.venv\Scripts\python.exe'
$desktopRoot = Join-Path $projectRoot 'desktop'
$flutterOutput = Join-Path $desktopRoot 'build\windows\x64\runner\Release'
$backendOutput = Join-Path $projectRoot 'dist\backend\synthv-backend'
if (-not (Get-Command $FlutterPath -ErrorAction SilentlyContinue)) {
    throw '未找到 Flutter，请使用 -FlutterPath 指定 flutter.bat。'
}
if (-not $SkipBackend) { & (Join-Path $PSScriptRoot 'package-desktop-backend.ps1') }
if (-not (Test-Path -LiteralPath (Join-Path $backendOutput 'synthv-backend.exe'))) {
    throw '缺少冻结后台，请先构建后台或移除 -SkipBackend。'
}
Push-Location -LiteralPath $desktopRoot
try {
    & $FlutterPath pub get
    if ($LASTEXITCODE -ne 0) { throw 'Flutter 依赖解析失败。' }
    # 将 AOT 调试符号留在本机专用构建目录，减少发行体积和源码调试信息。
    # Flutter 可能保留运行时插件注册所需的 URI，不能声称符号分离移除所有路径；
    # 发布前仍需核对构建目录不含个人名称、私有工程名称或其他敏感信息。
    $symbols = Join-Path $projectRoot ('build\desktop-symbols-' + [Guid]::NewGuid().ToString('N'))
    & $FlutterPath build windows --release --no-pub ('--split-debug-info=' + $symbols)
    if ($LASTEXITCODE -ne 0) { throw 'Flutter release 构建失败。' }
} finally { Pop-Location }

# 全新路径和显式源清单保证旧发行包不被覆盖，也不需要递归删除目录。
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$packageName = "SynthVAssistant-Windows-$stamp"
$packageRoot = Join-Path $projectRoot ("dist\" + $packageName)
if (Test-Path -LiteralPath $packageRoot) { throw '发行目录已存在，请稍后重试。' }
[void](New-Item -ItemType Directory -Path $packageRoot)
Get-ChildItem -LiteralPath $flutterOutput | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $packageRoot -Recurse
}
Copy-Item -LiteralPath $backendOutput -Destination (Join-Path $packageRoot 'backend') -Recurse
foreach ($document in @('LICENSE', 'THIRD_PARTY_NOTICES.md')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $document) -Destination $packageRoot
}
Copy-Item -LiteralPath (Join-Path $projectRoot 'docs\desktop-workbench.md') -Destination (Join-Path $packageRoot '使用说明.md')
[void](New-Item -ItemType Directory -Path (Join-Path $packageRoot 'scripts'))
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'install-desktop-bridge.ps1') -Destination (Join-Path $packageRoot 'scripts')
# WIL 由 Windows 插件通过 NuGet 获取，不在 Dart 的自动许可汇总范围中。
# 使用本次真实构建依赖的许可原文；缺失时中止发布，避免生成不完整声明。
$wilSource = Join-Path $desktopRoot 'build\windows\x64\packages\Microsoft.Windows.ImplementationLibrary'
$wilLicenses = Join-Path $packageRoot 'licenses\windows-implementation-library'
[void](New-Item -ItemType Directory -Path $wilLicenses -Force)
foreach ($notice in @('LICENSE', 'ThirdPartyNotices.txt')) {
    $source = Join-Path $wilSource $notice
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw ('缺少 WIL 原始许可：' + $notice)
    }
    Copy-Item -LiteralPath $source -Destination $wilLicenses
}
# Flutter 自动汇总的字体、框架和插件许可同时保留压缩原件与便于查看的文本。
# zip 使用相对路径，不含本机构建位置；校验摘要便于核对本地传输完整性。
& $pythonExecutable (Join-Path $PSScriptRoot 'package-desktop-archive.py') $packageRoot
if ($LASTEXITCODE -ne 0) { throw '发行包许可导出或压缩失败。' }
$archive = $packageRoot + '.zip'
$checksum = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath ($archive + '.sha256') -Value ($checksum + '  ' + [System.IO.Path]::GetFileName($archive)) -Encoding utf8
Write-Output ('桌面程序：' + (Join-Path $packageRoot 'synthv_desktop.exe'))
Write-Output ('完整发行包：' + $archive)
