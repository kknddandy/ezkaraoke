# Build a standalone ezkaraoke.exe for Windows with PyInstaller.
#
# Usage:  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# Output: dist\ezkaraoke\ezkaraoke.exe   (one-folder build)
#
# Requirements:
#   python -m pip install -e ".[dev]" pyinstaller
#   VLC media player installed (the exe uses the system libvlc at runtime;
#   VLC is NOT bundled).
#
# ffmpeg (with the rubberband filter) is optional: without it, pitch shift
# falls back to a tempo-changing speed shift.

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name ezkaraoke `
  --collect-data pypinyin `
  --collect-data ezkaraoke `
  ezkaraoke/main.py

Write-Host ""
Write-Host "Built: dist\ezkaraoke\ezkaraoke.exe"
Write-Host "Copy the whole dist\ezkaraoke folder to the target machine;"
Write-Host "VLC must be installed there."
