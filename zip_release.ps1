$ErrorActionPreference = "Stop"
$srcDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $srcDir
$exe = Join-Path $srcDir "dist\AniChDownloader.exe"

if (-not (Test-Path $exe)) {
    Write-Host "[1/3] 未找到 exe,安装 PyInstaller 并打包(约 1-3 分钟)..."
    pip install -U pyinstaller
    if ($LASTEXITCODE -ne 0) {
        Write-Host "PyInstaller 安装失败,请检查网络后重试。"
        Read-Host "按回车退出"
        exit 1
    }
    pyinstaller --noconfirm --clean --onefile --windowed --name AniChDownloader anich_gui.pyw
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $exe)) {
        Write-Host "打包失败,请把上面的报错截图发给我。"
        Read-Host "按回车退出"
        exit 1
    }
} else {
    Write-Host "[1/3] 已找到现成的 exe,跳过打包。"
}

$readme = @"
AniCh 动漫下载器 - 使用说明
============================

1. 双击 AniChDownloader.exe 启动(需要联网,无需安装 Python)。
2. 输入番剧名 -> 点[搜索] -> 双击结果加载剧集。
3. 右侧勾选要下载的集数(可 Ctrl/Shift 多选,或点[全选])。
4. 选中一集后点[刷新线路],从下拉框选线路(mp4 线路最省事)。
5. 点[浏览...]选择保存位置 -> 点[开始下载],失败会自动换线。
6. 若杀毒软件拦截,请选择"仍要运行/信任"(本程序为 Python 打包,误报常见)。
7. m3u8 线路需要额外安装 yt-dlp,建议优先选 mp4 线路。
"@
$readmePath = Join-Path $srcDir "使用说明.txt"
[System.IO.File]::WriteAllText($readmePath, $readme, (New-Object System.Text.UTF8Encoding($true)))

Write-Host "[2/3] 压缩为 zip ..."
$zip = Join-Path $srcDir "AniCh下载器-便携版.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $exe, $readmePath -DestinationPath $zip
Write-Host "[3/3] 完成!"
Write-Host "文件位置: $zip"
Write-Host "把这个 zip 发给别人,解压后双击 AniChDownloader.exe 即可使用。"
Read-Host "按回车退出"