<#
.SYNOPSIS
  Instala o Bira Flow num ambiente virtual (.venv) e cria um atalho no Menu Iniciar.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#>
param([switch]$NoShortcut)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Criando ambiente virtual..."
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv } else { python -m venv .venv }
}
$python = Join-Path $root ".venv\Scripts\python.exe"
& $python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw "O Bira Flow precisa do Python 3.11 ou mais novo." }

Write-Host "Instalando dependências..."
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -e .
if ($LASTEXITCODE -ne 0) { throw "pip install falhou." }

$data = Join-Path $env:APPDATA "BiraFlow"
New-Item -ItemType Directory -Force $data | Out-Null
$envFile = Join-Path $data ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root ".env.example") $envFile
    Write-Host "Arquivo de chaves criado: $envFile"
}

$launcher = Join-Path $root ".venv\Scripts\biraflow.exe"
if (-not $NoShortcut) {
    # "(dev)": the installer owns "Bira Flow.lnk"; this one points at the source folder
    $lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "Bira Flow (dev).lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($lnk)
    $shortcut.TargetPath = $launcher
    $shortcut.WorkingDirectory = $root
    $shortcut.Description = "Ditado por voz em qualquer app"
    $shortcut.IconLocation = (Join-Path $root "biraflow\assets\app.ico") + ",0"
    $shortcut.Save()
    $old = Join-Path ([Environment]::GetFolderPath("Programs")) "Whisperflow.lnk"
    if (Test-Path $old) { Remove-Item $old }  # name before the rename
    Write-Host "Atalho criado no Menu Iniciar: Bira Flow"
}

Write-Host ""
Write-Host "Pronto. Próximos passos:"
Write-Host "  1. Modelo local:  powershell -ExecutionPolicy Bypass -File scripts\setup-whisper.ps1"
Write-Host "  2. Diagnóstico:   .venv\Scripts\python -m biraflow --check"
Write-Host "  3. Iniciar:       $launcher   (ou 'Bira Flow' no Menu Iniciar)"
