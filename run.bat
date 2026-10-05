@echo off
rem betterGeoPort launcher for Windows. iOS 17+ location-setting needs
rem Administrator; main.py asks for it via the UAC prompt.
cd /d "%~dp0"
".venv\Scripts\python.exe" main.py %*
