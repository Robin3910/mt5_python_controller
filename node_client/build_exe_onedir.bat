@echo off
REM 将 node_client 打包为 Windows 可执行程序（PyInstaller onedir 目录模式）
REM 产物: dist\node_client\ 目录（内含 node_client.exe 及依赖文件）
REM 版本号每次打包自动生成: ${数字版本号}-${年月日时分秒}（数字版本号见 version.py）
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

REM 先打版本戳：_build_info.py 必须早于 PyInstaller 分析生成，才能冻结进 exe
echo Stamping build version ...
set "APPVER="
for /f "usebackq delims=" %%v in (`python build_version.py stamp`) do set "APPVER=%%v"
if not defined APPVER (
    echo Failed to stamp build version.
    exit /b 1
)
echo   Version: %APPVER%

echo Building node_client (onedir) ...
if exist "dist\node_client.exe" (
    echo Removing previous onefile build: dist\node_client.exe
    del /q "dist\node_client.exe"
)
if exist "dist\node_client" (
    echo Removing previous onedir build: dist\node_client\
    rmdir /s /q "dist\node_client"
)
pyinstaller --noconfirm --clean node_client_onedir.spec
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

set "OUT=dist\node_client"
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

python build_version.py write "%OUT%"
if errorlevel 1 (
    echo Failed to write %OUT%\version.txt
    exit /b 1
)

REM 顺手打一份安装包（onedir 形态整棵目录树入包）；.env 不会入包
echo Packaging installer zip ...
set "PKG="
for /f "usebackq tokens=1 delims=|" %%p in (`python build_package.py "%OUT%" "packages"`) do set "PKG=%%p"
if not defined PKG (
    echo Packaging failed.
    exit /b 1
)

echo.
echo Build successful.
echo   Directory:  %OUT%\
echo   Executable: %EXE%
echo   Config:     %OUT%\.env
echo   Version:    %APPVER%
echo   Stamp file: %OUT%\version.txt
echo   Package:    %PKG%
echo.
echo Upload the package on the admin site: Client Versions - Upload
echo.
echo Place .env next to the exe and run:
echo   cd %OUT%
echo   node_client.exe

endlocal
