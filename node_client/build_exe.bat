@echo off
REM 将 node_client 打包为 Windows 可执行程序（PyInstaller onedir）
REM 产物目录: dist\node_client\
REM 运行前请编辑 dist\node_client\.env（首次构建会从 .env.example 复制）
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

echo Building node_client.exe ...
pyinstaller --noconfirm --clean node_client.spec
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

set "OUT=dist\node_client"
if not exist "%OUT%\node_client.exe" (
    echo Build failed: %OUT%\node_client.exe not found.
    exit /b 1
)

if not exist "%OUT%\.env" (
    if exist .env (
        copy /Y .env "%OUT%\.env"
        echo Copied local .env to %OUT%\.env
    ) else (
        copy /Y .env.example "%OUT%\.env"
        echo Copied .env.example to %OUT%\.env
    )
) else (
    echo Keeping existing %OUT%\.env
)

echo.
echo Build successful.
echo   Executable: %OUT%\node_client.exe
echo   Config:     %OUT%\.env
echo.
echo Run from the output folder so .env is found:
echo   cd %OUT%
echo   node_client.exe

endlocal
