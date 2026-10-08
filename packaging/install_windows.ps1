# Add betterGeoPort to the Start menu and to Settings > Apps > Installed apps.
# Per-user: copies the app to %LOCALAPPDATA%\Programs\betterGeoPort, so no
# Administrator rights are needed to install (the app itself still asks for them).
#   From the repo, after build_windows.ps1:
#     powershell -ExecutionPolicy Bypass -File packaging\install_windows.ps1
#   From the extracted zip: right-click install_windows.ps1 > Run with PowerShell
# Run it again after a rebuild to update. Uninstall from Settings > Apps, or run
# the installed copy with -Uninstall. Settings and the map cache in
# %USERPROFILE%\GeoPort are kept either way.
param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'

$Name = 'betterGeoPort'
$Dir = Join-Path $env:LOCALAPPDATA "Programs\$Name"
$Exe = Join-Path $Dir "$Name.exe"
$Shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) "$Name.lnk"
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$Name"

if (Get-Process $Name -ErrorAction SilentlyContinue) {
    throw "$Name is running. Quit it first (close its console window, or Exit in the page)."
}

if ($Uninstall) {
    if (Test-Path $Shortcut) { Remove-Item $Shortcut }
    if (Test-Path $UninstallKey) { Remove-Item $UninstallKey -Recurse }
    if (Test-Path $Dir) {
        # Can't delete the folder we're standing in (the process's directory too,
        # which Set-Location doesn't change).
        Set-Location $env:TEMP
        [Environment]::CurrentDirectory = $env:TEMP
        Remove-Item $Dir -Recurse -Force
    }
    Write-Host "$Name uninstalled. Settings and map cache in $env:USERPROFILE\GeoPort were kept."
    return
}

# Install from the folder holding betterGeoPort.exe: this script's own folder
# (the zip, or the installed copy), else the repo's build output.
$Src = if (Test-Path (Join-Path $PSScriptRoot "$Name.exe")) { $PSScriptRoot }
       else { Join-Path (Split-Path -Parent $PSScriptRoot) "dist\$Name" }
if (-not (Test-Path (Join-Path $Src "$Name.exe"))) {
    throw "No $Name.exe found. Build it first:  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1"
}
$Src = (Resolve-Path $Src).Path

# Running the installed copy again only re-registers it.
if ($Src -ne $Dir) {
    Write-Host "Copying $Src to $Dir..."
    if (Test-Path $Dir) { Remove-Item $Dir -Recurse -Force }
    New-Item -ItemType Directory -Force (Split-Path -Parent $Dir) | Out-Null
    Copy-Item $Src $Dir -Recurse
    # Kept next to the app for Settings' Uninstall button.
    Copy-Item $PSCommandPath (Join-Path $Dir 'install_windows.ps1') -Force
}

$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($Shortcut)
$lnk.TargetPath = $Exe
$lnk.WorkingDirectory = $Dir
$lnk.Description = 'Simulate the GPS location of your iPhone or iPad'
$lnk.Save()

$SizeKB = [int]((Get-ChildItem $Dir -Recurse -File | Measure-Object Length -Sum).Sum / 1KB)
New-Item $UninstallKey -Force | Out-Null
$Strings = @{
    DisplayName     = $Name
    DisplayIcon     = $Exe
    InstallLocation = $Dir
    URLInfoAbout    = 'https://github.com/Kyomte/betterGeoPort'
    UninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Dir\install_windows.ps1`" -Uninstall"
}
foreach ($k in $Strings.Keys) {
    New-ItemProperty $UninstallKey -Name $k -Value $Strings[$k] -PropertyType String | Out-Null
}
New-ItemProperty $UninstallKey -Name EstimatedSize -Value $SizeKB -PropertyType DWord | Out-Null
New-ItemProperty $UninstallKey -Name NoModify -Value 1 -PropertyType DWord | Out-Null
New-ItemProperty $UninstallKey -Name NoRepair -Value 1 -PropertyType DWord | Out-Null

Write-Host "Installed: $Name is in the Start menu and in Settings > Apps > Installed apps."
