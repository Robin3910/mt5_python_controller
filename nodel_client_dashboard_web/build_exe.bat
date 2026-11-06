@echo off
REM Build the WebView dashboard into a Windows GUI executable (PyInstaller onefile).
REM Outputs: dist\*.exe (the exe name is set by dashboard_web.spec).
REM
REM This is the WebView2 variant; the classic Tk dashboard builds separately from
REM nodel_client_dashboard\build_exe.bat. Both can sit in the same folder and
REM share instances.json / panel_config.json / .env.
REM
REM Keep this file ASCII-only with CRLF line endings. cmd.exe parses .bat by CRLF, and it
REM resolves redirection before REM, so non-ASCII comments (mis-decoded under the system
REM code page) or a bare < > | in a comment will break the script or emit stray errors.
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

REM Build the frontend first: the spec embeds frontend-console\dist.
set "FRONTEND=%~dp0..\frontend-console"
if exist "%FRONTEND%\node_modules" (
    echo Building frontend ...
    pushd "%FRONTEND%"
    call npm run build:only
    if errorlevel 1 (
        popd
        echo Frontend build failed.
        exit /b 1
    )
    popd
) else (
    echo WARNING: frontend-console\node_modules missing. Run "npm install" in frontend-console.
)

REM Stamp first: _build_info.py must exist before PyInstaller analysis to get frozen in.
echo Stamping build version ...
set "APPVER="
for /f "usebackq delims=" %%v in (`python build_version.py stamp`) do set "APPVER=%%v"
if not defined APPVER (
    echo Failed to stamp build version.
    exit /b 1
)
echo   Version: %APPVER%

echo Building ...
pyinstaller --noconfirm --clean dashboard_web.spec
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

set "OUT=dist"
REM The spec names the exe in Chinese, which cmd.exe mis-decodes under the system
REM code page. Locate it by globbing instead of hardcoding the name.
set "EXE_NAME="
for %%f in ("%OUT%\*.exe") do set "EXE_NAME=%%~nxf"
if not defined EXE_NAME (
    echo Build failed: no exe found in %OUT%.
    exit /b 1
)
set "EXE=%OUT%\%EXE_NAME%"
if not exist "%EXE%" (
    echo Build failed: %EXE% not found.
    exit /b 1
)

python build_version.py write "%OUT%"
if errorlevel 1 (
    echo Failed to write %OUT%\version.txt
    exit /b 1
)

REM Stage the portable folder: exe + .env + version.txt + the bundled WebView2 runtime.
REM The runtime must ship NEXT TO THE EXE. Windows Server 2022 has no preinstalled
REM WebView2 Runtime; without it pywebview silently falls back to MSHTML (IE),
REM which cannot run our Vue 3 frontend -> blank window.
REM stage_portable.py also verifies the pruned runtime really starts, and exits
REM non-zero if it does not - so a broken build cannot be shipped.
if exist "..\.toolchain\wv2_rt" (
    echo Staging portable folder with bundled WebView2 runtime ...
    python stage_portable.py
    if errorlevel 1 (
        echo Failed to stage portable folder.
        exit /b 1
    )
) else (
    echo WARNING: ..\.toolchain\wv2_rt not found - portable folder will have NO bundled runtime.
    echo          Machines without WebView2 Runtime will show a blank window.
)

echo.
echo Build successful.
echo   Executable: %EXE%
echo   Version:    %APPVER%
echo.
echo Requirements on target machine:
echo   - Edge WebView2 Runtime: optional if the portable folder includes webview2\,
echo     otherwise REQUIRED (Windows Server 2022 does not ship it).
echo   - .NET Framework 4.8 (ships with Windows)
echo.
echo The exe reads instances.json / panel_config.json / .env from its own folder,
echo so place it next to the classic dashboard to share the same instance list.

endlocal
