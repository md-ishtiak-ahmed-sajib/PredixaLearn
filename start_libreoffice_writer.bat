@echo off
setlocal
set "ROOT=%~dp0"
set "PREDIXALEARN_TOOLS_ROOT=%ROOT%.tools"
call "%ROOT%apps\ocr-studio\start_libreoffice_writer.bat" %*
exit /b %ERRORLEVEL%
