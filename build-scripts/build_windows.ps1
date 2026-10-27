# Builds dist\KeychronNapeBatteryWidget.exe (single file, no console window).
# Run from the repository root in PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\build-scripts\build_windows.ps1
# Requires Python 3.10+ on PATH.
$HidapiVersion = "0.15.0"

# pip and PyInstaller log to stderr, so check exit codes rather than using
# $ErrorActionPreference = "Stop", which aborts on any stderr output when redirected.
function Invoke-Checked {
    & $args[0] $args[1..($args.Length - 1)]
    if ($LASTEXITCODE -ne 0) { throw "failed ($LASTEXITCODE): $args" }
}

Invoke-Checked python -m venv .venv-win
Invoke-Checked .\.venv-win\Scripts\python -m pip install --quiet --upgrade pip
Invoke-Checked .\.venv-win\Scripts\python -m pip install --quiet -r requirements.txt pyinstaller

# The 'hid' package is a ctypes wrapper; it needs hidapi.dll at runtime.
if (-not (Test-Path build\hidapi\x64\hidapi.dll)) {
    New-Item -ItemType Directory -Force build | Out-Null
    Invoke-WebRequest "https://github.com/libusb/hidapi/releases/download/hidapi-$HidapiVersion/hidapi-win.zip" -OutFile build\hidapi-win.zip
    Expand-Archive build\hidapi-win.zip -DestinationPath build\hidapi -Force
}

Invoke-Checked .\.venv-win\Scripts\pyinstaller --noconfirm --clean --onefile --windowed `
    --name KeychronNapeBatteryWidget `
    --icon widget_logo.ico `
    --paths src `
    --add-binary "build\hidapi\x64\hidapi.dll;." `
    --exclude-module PyQt6.QtQml --exclude-module PyQt6.QtQuick --exclude-module PyQt6.QtPdf `
    src\nape_widget.py

Write-Host "Built dist\KeychronNapeBatteryWidget.exe"
