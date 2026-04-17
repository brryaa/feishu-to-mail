@echo off
setlocal
cd /d "%~dp0"
if /I "%1"=="mailer" (
  python file_receiver_and_mailer.py
) else if /I "%1"=="compatible" (
  python main_compatible.py
) else (
  python main.py --config config.json
)
endlocal
