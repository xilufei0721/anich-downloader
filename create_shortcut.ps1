# 创建 AniCh 下载器桌面快捷方式
$ErrorActionPreference = "Stop"
$srcDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe = Join-Path $srcDir "dist\AniChDownloader.exe"
$desktop = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "AniCh 动漫下载器.lnk"

$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($linkPath)

if (Test-Path $exe) {
    $shortcut.TargetPath = $exe
    $shortcut.WorkingDirectory = Split-Path -Parent $exe
    Write-Host "使用已打包的 exe: $exe"
} else {
    $pyw = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\pythonw.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $pyw) {
        $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
        if ($cmd) { $pyw = $cmd.Source }
    }
    if (-not $pyw) {
        Write-Host "未找到 pythonw.exe,请先安装 Python 或先运行 build.bat 打包 exe。"
        Read-Host "按回车退出"
        exit 1
    }
    $shortcut.TargetPath = $pyw
    $shortcut.Arguments = '"' + (Join-Path $srcDir "anich_gui.pyw") + '"'
    $shortcut.WorkingDirectory = $srcDir
    $shortcut.IconLocation = "$pyw,0"
    Write-Host "使用脚本版: $pyw anich_gui.pyw"
}

$shortcut.Description = "AniCh 动漫下载器"
$shortcut.Save()
Write-Host "已创建桌面快捷方式: $linkPath"
Write-Host "提示: 之后若用 build.bat 打包出 exe,再运行一次本脚本即可自动切换为 exe 版。"
Read-Host "按回车退出"