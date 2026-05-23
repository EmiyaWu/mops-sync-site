$ErrorActionPreference = "Stop"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller

pyinstaller `
  --onefile `
  --name mops_sync `
  --collect-all gspread `
  --collect-all google.auth `
  mos_s.py

Write-Host ""
Write-Host "Build complete: dist\mops_sync.exe"
Write-Host "Run example:"
Write-Host '$env:GOOGLE_APPLICATION_CREDENTIALS="D:\Code\py_MOS\service-account.json"'
Write-Host ".\dist\mops_sync.exe validate"
Write-Host ".\dist\mops_sync.exe run"
