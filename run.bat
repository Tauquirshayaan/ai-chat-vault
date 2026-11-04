@echo off
chcp 65001 >nul 2>&1
python "%~dp0chat_vault.py" %*
if errorlevel 9009 (
  echo Python 3.8+ is required: https://www.python.org/downloads/
  pause
)
