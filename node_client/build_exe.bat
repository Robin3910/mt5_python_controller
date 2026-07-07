@echo off
REM 将 node_client 打包为 Windows 可执行程序（PyInstaller onefile 单文件）
REM 产物: dist\node_client.exe
REM 运行前请编辑 dist\.env（首次构建会从 .env.example 复制）
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
if exist "dist\node_client" (
    echo Removing previous onedir build: dist\node_client\
    rmdir /s /q "dist\node_client"
)
pyinstaller --noconfirm --clean node_client.spec
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

set "OUT=dist"
set "EXE=%OUT%\node_client.exe"
if not exist "%EXE%" (
    echo Build failed: %EXE% not found.
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
echo   Executable: %EXE%
echo   Config:     %OUT%\.env
echo.
echo Place .env next to the exe and run:
echo   cd %OUT%
echo   node_client.exe

endlocal
