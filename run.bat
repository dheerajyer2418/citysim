@echo off
REM CitySim one-step launcher. Double-click this file to start the app,
REM or run it from a terminal (any cli.py serve flags pass through):
REM     run.bat --port 8010
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
