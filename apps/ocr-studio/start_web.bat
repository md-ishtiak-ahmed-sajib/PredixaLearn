@echo off
setlocal
set "PROJECT_ROOT=%~dp0"
if defined PREDIXALEARN_PYTHON (
  set "PYTHON=%PREDIXALEARN_PYTHON%"
) else (
  set "PYTHON=%PROJECT_ROOT%.venv\Scripts\python.exe"
)

if not exist "%PYTHON%" (
  echo [ERROR] Virtual environment not found.
  echo Run: powershell -ExecutionPolicy Bypass -File "%PROJECT_ROOT%setup_env.ps1"
  exit /b 1
)

if defined OCR_MAINTENANCE_DIR (
  set "MAINTENANCE_ROOT=%OCR_MAINTENANCE_DIR%"
) else (
  set "MAINTENANCE_ROOT=%LOCALAPPDATA%\PredixaLearn\maintenance"
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%scripts\complete_maintenance_update.ps1" -MaintenanceRoot "%MAINTENANCE_ROOT%"
if errorlevel 1 exit /b %ERRORLEVEL%

"%PYTHON%" "%PROJECT_ROOT%run.py" %*
endlocal
