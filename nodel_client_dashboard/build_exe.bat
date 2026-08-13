@echo off
REM 将运维面板打包为 Windows GUI 可执行程序（PyInstaller onefile）
REM 产物: dist\node_client_dashboard.exe + 分发压缩包 packages\node_client_dashboard-<版本>.zip
REM 版本号每次打包自动生成: ${数字版本号}-${年月日时分秒}（数字版本号见 version.py）
setlocal
cd /d %~dp0

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

echo Installing dependencies...
pip install -q -r requirements.txt
pip install -q "pyinstaller>=6.0"

REM 先打版本戳：_build_info.py 必须早于 PyInstaller 分析生成，才能冻结进 exe
echo Stamping build version ...
set "APPVER="
for /f "usebackq delims=" %%v in (`python build_version.py stamp`) do set "APPVER=%%v"
if not defined APPVER (
    echo Failed to stamp build version.
    exit /b 1
)
echo   Version: %APPVER%

echo Building node_client_dashboard.exe ...
pyinstaller --noconfirm --clean dashboard.spec
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

set "OUT=dist"
set "EXE=%OUT%\node_client_dashboard.exe"
if not exist "%EXE%" (
    echo Build failed: %EXE% not found.
    exit /b 1
)

python build_version.py write "%OUT%"
if errorlevel 1 (
    echo Failed to write %OUT%\version.txt
    exit /b 1
)

REM 打一份分发压缩包；面板的 panel_config.json / instances.json / logs 不会入包
echo Packaging distribution zip ...
set "PKG="
for /f "usebackq tokens=1 delims=|" %%p in (`python build_package.py "%OUT%" "packages"`) do set "PKG=%%p"
if not defined PKG (
    echo Packaging failed.
    exit /b 1
)

echo.
echo Build successful.
echo   Executable: %EXE%
echo   Version:    %APPVER%
echo   Stamp file: %OUT%\version.txt
echo   Package:    %PKG%
echo   Config will be written next to the exe as instances.json
echo   Logs:       %OUT%\logs\
echo.
echo Run:
echo   cd %OUT%
echo   node_client_dashboard.exe

endlocal
