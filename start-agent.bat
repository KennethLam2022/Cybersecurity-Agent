@echo off
title NetSec AI Agent Service

echo ============================================
echo   NetSec AI Agent - Starting...
echo ============================================
echo.

set "PY=D:\Programs\Python\Python312\python.exe"
set "DIR=%~dp0"

if not exist "%DIR%packages\agent\src\main.py" (
    echo [ERROR] main.py not found at:
    echo   %DIR%packages\agent\src\main.py
    echo.
    pause
    exit /b 1
)

echo [INFO] Project: %DIR%
echo [INFO] Python : %PY%
echo.
echo [TIP] Open in browser after startup:
echo       http://127.0.0.1:8000/admin
echo       http://127.0.0.1:8000
echo.
echo [TIP] Press Ctrl+C to stop
echo ============================================
echo.

cd /d "%DIR%packages\agent\src"
"%PY%" main.py
if %errorlevel% neq 0 (
    echo [ERROR] Server exited with code: %errorlevel%
    pause
)