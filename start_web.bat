@echo off
setlocal
set "ROOT=%~dp0"
set "PREDIXALEARN_PYTHON=%ROOT%.venv\Scripts\python.exe"
set "PREDIXALEARN_TOOLS_ROOT=%ROOT%.tools"
call "%ROOT%apps\ocr-studio\start_web.bat" %*
exit /b %ERRORLEVEL%
