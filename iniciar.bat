@echo off
rem Abre el dashboard de respuestas en el navegador. Cerrar esta ventana apaga el dashboard.
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=py"
"%PY%" server.py
pause
