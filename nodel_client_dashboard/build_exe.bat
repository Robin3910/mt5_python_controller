@echo off
REM Build the dashboard into a Windows GUI executable (PyInstaller onefile).
REM Outputs: dist\node_client_dashboard.exe and packages\node_client_dashboard-VERSION.zip
REM Version is generated per build as ${base}-${YYYYmmddHHMMSS}; base lives in version.py.
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

REM Stamp first: _build_info.py must exist before PyInstaller analysis to get frozen in.
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

REM Distribution zip; panel_config.json / instances.json / logs are never packed.
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
