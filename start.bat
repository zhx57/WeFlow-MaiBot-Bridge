@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python virtual environment was not found.
  echo Run this command in the project directory:
  echo   py -3.12 -m venv .venv
  echo Then run start.bat again.
  pause
  exit /b 1
)
if not exist "config.toml" (
  echo [ERROR] config.toml was not found.
  echo Run this command in the project directory:
  echo   copy config.example.toml config.toml
  echo Then edit config.toml and run start.bat again.
  pause
  exit /b 1
)
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
".venv\Scripts\python.exe" -c "import maim_message, requests, uiautomation, pyperclip; assert maim_message.__version__ == '0.6.8'" >nul 2>&1
if errorlevel 1 (
  echo [INFO] Installing compatible dependencies. This may take a few minutes...
  echo [INFO] MaiBot 1.0.12 requires maim-message 0.6.8. Version 0.7.x causes HTTP 404.
  ".venv\Scripts\python.exe" -m pip install -e .
  if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    echo Check the network and run this command manually:
    echo   ".venv\Scripts\python.exe" -m pip install -e .
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -m weflow_maibot_bridge --config config.toml
set code=%errorlevel%
echo Bridge exited with code %code%.
pause
exit /b %code%
