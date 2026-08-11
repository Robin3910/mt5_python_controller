@echo off
REM 将运维面板打包为 Windows GUI 可执行程序（PyInstaller onefile）
REM 产物: dist\node_client_dashboard.exe
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

echo.
echo Build successful.
echo   Executable: %EXE%
echo   Config will be written next to the exe as instances.json
echo   Logs:       %OUT%\logs\
echo.
echo Run:
echo   cd %OUT%
echo   node_client_dashboard.exe

endlocal
