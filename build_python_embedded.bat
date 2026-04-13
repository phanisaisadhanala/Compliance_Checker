@echo off
REM ════════════════════════════════════════════════════════════════════════════
REM  PhasorGrid — Embedded Python Builder
REM  Run this ONCE on your build machine before running: npm run build
REM  It downloads Python embeddable, installs pip, installs all dependencies.
REM  Output: python-embedded\  (referenced in package.json extraResources)
REM ════════════════════════════════════════════════════════════════════════════

setlocal enabledelayedexpansion
set PY_VERSION=3.11.8
set PY_URL=https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-embed-amd64.zip
set GET_PIP=https://bootstrap.pypa.io/get-pip.py
set OUT_DIR=python-embedded

echo [1/5] Creating output directory...
if exist "%OUT_DIR%" rmdir /s /q "%OUT_DIR%"
mkdir "%OUT_DIR%"

echo [2/5] Downloading Python %PY_VERSION% embeddable...
powershell -Command "Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%OUT_DIR%\python-embed.zip'"
powershell -Command "Expand-Archive -Path '%OUT_DIR%\python-embed.zip' -DestinationPath '%OUT_DIR%'"
del "%OUT_DIR%\python-embed.zip"

echo [3/5] Enabling site-packages in embeddable Python...
REM Uncomment the import site line in python311._pth
for %%f in ("%OUT_DIR%\python*._pth") do (
    powershell -Command "(Get-Content '%%f') -replace '#import site','import site' | Set-Content '%%f'"
)

echo [4/5] Installing pip into embedded Python...
powershell -Command "Invoke-WebRequest -Uri '%GET_PIP%' -OutFile '%OUT_DIR%\get-pip.py'"
"%OUT_DIR%\python.exe" "%OUT_DIR%\get-pip.py" --no-warn-script-location
del "%OUT_DIR%\get-pip.py"

echo [5/5] Installing required packages...
"%OUT_DIR%\python.exe" -m pip install flask flask-cors olefile openpyxl --no-warn-script-location

echo.
echo ========================================================
echo  Done! python-embedded\ is ready.
echo  Now run:  npm run build
echo ========================================================
pause
