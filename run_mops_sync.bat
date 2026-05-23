@echo off
setlocal
cd /d "%~dp0"

if exist "%~dp0mops_sync.exe" (
  "%~dp0mops_sync.exe" run
) else (
  python "%~dp0mos_s.py" run
)

pause
