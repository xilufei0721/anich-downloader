@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] 安装 PyInstaller ...
pip install -U pyinstaller
if errorlevel 1 (
  echo PyInstaller 安装失败,请检查网络后重试。
  pause
  exit /b 1
)
echo [2/2] 打包中,请稍候(约 1-3 分钟) ...
pyinstaller --noconfirm --clean --onefile --windowed --name AniChDownloader anich_gui.pyw
if errorlevel 1 (
  echo 打包失败,请把上面的报错信息发给我。
  pause
  exit /b 1
)
echo.
echo 完成!程序位置: dist\AniChDownloader.exe
echo 可以把 exe 复制到任意位置双击运行(需联网)。
pause