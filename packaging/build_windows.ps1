# Build betterGeoPort for Windows from source.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# Produces dist\betterGeoPort\betterGeoPort.exe and betterGeoPort-windows-x64.zip.
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) {
    throw "No .venv found. Create it first:  py -3.12 -m venv .venv; .venv\Scripts\pip install -r requirements.txt pyinstaller"
}
$Ver = & $Py -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($Ver -notin @('3.11', '3.12')) {
    throw "Python $Ver in .venv; Windows builds need 3.11 or 3.12 (sslpsk-pmd3 has no newer Windows wheels)."
}
& $Py -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) { & $Py -m pip install pyinstaller }

Write-Host '[1/3] PyInstaller build...'
& $Py -m PyInstaller --noconfirm --clean GeoPort.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }

$Out = Join-Path $Root 'dist\betterGeoPort'
Write-Host '[2/3] Adding README-Windows.txt and install_windows.ps1...'
Copy-Item (Join-Path $PSScriptRoot 'install_windows.ps1') $Out
@'
betterGeoPort for Windows
=========================

Before first use
  1. Install the "Apple Devices" app from the Microsoft Store (or iTunes).
     It provides Apple Mobile Device Service, which betterGeoPort needs to
     see your iPhone/iPad.
  2. On the device: Settings > Privacy & Security > Developer Mode > On
     (the device restarts). Connect it by USB, unlock it, tap Trust.

Run
  Double-click betterGeoPort.exe and accept the Windows (UAC) prompt -
  iOS 17+ location tunnels need Administrator rights. Your browser opens
  http://localhost:54321 automatically.

  Keep the console window open while you use it; close it (or press Exit
  in the page) to quit.

Add to the Start menu (optional)
  Right-click install_windows.ps1 > Run with PowerShell. It copies
  betterGeoPort to your user folder and adds it to the Start menu and to
  Settings > Apps > Installed apps, where you can uninstall it. After
  that you can delete this folder.

Notes
  * Windows SmartScreen may warn because the app is not code-signed:
    "More info" > "Run anyway".
  * Logs and the offline map cache live in %USERPROFILE%\GeoPort.
'@ | Set-Content -Encoding utf8 (Join-Path $Out 'README-Windows.txt')

Write-Host '[3/3] Zipping...'
$Zip = Join-Path $Root 'betterGeoPort-windows-x64.zip'
if (Test-Path $Zip) { Remove-Item $Zip }
Compress-Archive -Path $Out -DestinationPath $Zip
Write-Host "Done: $Out\betterGeoPort.exe"
Write-Host "      $Zip"
