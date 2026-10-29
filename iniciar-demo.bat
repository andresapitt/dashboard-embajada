@echo off
rem Abre el dashboard con datos de EJEMPLO (base aparte: datos\demo.db). No toca tus datos reales.
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=py"
set "DASHBOARD_DB=%~dp0datos\demo.db"
set "DASHBOARD_PUERTO=8766"
set "DASHBOARD_DEMO=1"
"%PY%" demo.py
"%PY%" server.py
pause
