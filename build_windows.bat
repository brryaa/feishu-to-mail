@echo off
setlocal
cd /d "%~dp0"
pyinstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --name FeishuFileMailer ^
  main.py
endlocal
