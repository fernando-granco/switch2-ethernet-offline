# SPDX-License-Identifier: GPL-2.0-or-later
# Builds dist\SwitchOfflineEthernet.exe: one file, windowed, asks for admin (UAC).
# Requires Python 3.9+ with tkinter, and: python -m pip install pyinstaller
# -OutDir puts the exe elsewhere, e.g. while the one in dist is running.
param([string]$OutDir = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist'))
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$icon = Join-Path $root 'switch2_offline\app.ico'
$build = Join-Path $root 'build'

python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin `
    --name SwitchOfflineEthernet --icon $icon `
    --add-data "$icon;switch2_offline" `
    --hidden-import switch2_offline.gui `
    --paths $root --distpath $OutDir --workpath $build --specpath $build `
    (Join-Path $PSScriptRoot 'exe_entry.py')
if ($LASTEXITCODE) { throw "PyInstaller failed (exit $LASTEXITCODE)" }

$exe = Get-Item (Join-Path $OutDir 'SwitchOfflineEthernet.exe')
'{0}  {1:N0} bytes  SHA-256 {2}' -f $exe.Name, $exe.Length, (Get-FileHash $exe).Hash
