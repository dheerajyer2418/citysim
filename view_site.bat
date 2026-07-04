@echo off
REM Double-click to preview the CitySim website locally in your browser.
REM (Static preview of what is deployed to Vercel; no Java needed.)
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0viz\serve_site.py"
