@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run: py -3.12 -m venv .venv
  exit /b 1
)
if not exist "config.toml" (
  echo [ERROR] config.toml not found. Create it from config.example.toml.
  exit /b 1
)
".venv\Scripts\python.exe" -m weflow_maibot_bridge --config config.toml
set code=%errorlevel%
echo Bridge exited with code %code%.
pause
exit /b %code%
