<#
.SYNOPSIS
  Installs Bira Flow in a virtual environment (.venv) and creates a Start Menu shortcut.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#>
param([switch]$NoShortcut)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating the virtual environment..."
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv } else { python -m venv .venv }
}
$python = Join-Path $root ".venv\Scripts\python.exe"
& $python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Bira Flow needs Python 3.11 or newer." }

Write-Host "Installing the dependencies..."
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -e .
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

$data = Join-Path $env:APPDATA "BiraFlow"
New-Item -ItemType Directory -Force $data | Out-Null
$envFile = Join-Path $data ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root ".env.example") $envFile
    Write-Host "Keys file created: $envFile"
}

$launcher = Join-Path $root ".venv\Scripts\biraflow.exe"
if (-not $NoShortcut) {
    # "(dev)": the installer owns "Bira Flow.lnk"; this one points at the source folder
    $lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "Bira Flow (dev).lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($lnk)
    $shortcut.TargetPath = $launcher
    $shortcut.WorkingDirectory = $root
    $shortcut.Description = "Voice dictation for every app"
    $shortcut.IconLocation = (Join-Path $root "biraflow\assets\app.ico") + ",0"
    $shortcut.Save()
    $old = Join-Path ([Environment]::GetFolderPath("Programs")) "Whisperflow.lnk"
    if (Test-Path $old) { Remove-Item $old }  # name before the rename
    Write-Host "Shortcut created in the Start Menu: Bira Flow"
}

Write-Host ""
Write-Host "Done. Next steps:"
Write-Host "  1. Local model:  powershell -ExecutionPolicy Bypass -File scripts\setup-whisper.ps1"
Write-Host "  2. Diagnostic:   .venv\Scripts\python -m biraflow --check"
Write-Host "  3. Start:        $launcher   (or 'Bira Flow' in the Start Menu)"
