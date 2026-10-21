<#
.SYNOPSIS
  Gera o instalador do Bira Flow: dist\BiraFlow-Setup-<versão>.exe e dist\latest.json.
.PARAMETER Repo
  Repositório do GitHub cujos Releases servem as atualizações automáticas (ex.: usuario/bira-flow).
  Fica salvo em packaging\release.json para os próximos builds.
.PARAMETER Unsigned
  Gera o instalador sem assinatura (builds de teste e CI). A versão assim não atualiza
  sozinha: o atualizador exige um certificado fixado.
.PARAMETER SignerThumbprint
  Impressão digital do certificado que assina o instalador, quando ele não é o local
  (a SignPath Foundation assina na CI e o certificado é público).
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\build-installer.ps1 -Repo usuario/bira-flow
#>
param([string]$Repo = "", [string]$Notes = "", [switch]$Unsigned, [string]$SignerThumbprint = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    # no virtual environment (the CI installs the dependencies in the runner's Python)
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) { throw "Python not found: install it or run scripts\install.ps1 first" }
}

# --- tools -------------------------------------------------------------------------
function Find-Iscc {
    @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
      "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
$iscc = Find-Iscc
if (-not $iscc) {
    Write-Host "Instalando o Inno Setup (gratuito)..."
    winget install -e --id JRSoftware.InnoSetup --scope user --silent --accept-package-agreements --accept-source-agreements
    $iscc = Find-Iscc
    if (-not $iscc) { throw "Inno Setup não encontrado. Instale em https://jrsoftware.org/isdl.php" }
}
& $python -m pip install --quiet "pyinstaller>=6.10"
$localPfx = Join-Path $root "packaging\signing\biraflow-codesign.pfx"
if (-not $Unsigned -and -not $SignerThumbprint -and -not (Test-Path $localPfx)) {
    & "$PSScriptRoot\new-signing-cert.ps1"
}

# --- release settings ------------------------------------------------------------------
$releaseFile = Join-Path $root "packaging\release.json"
if ($Repo) { @{ repo = $Repo } | ConvertTo-Json | Set-Content $releaseFile -Encoding utf8 }
elseif (Test-Path $releaseFile) { $Repo = (Get-Content $releaseFile -Raw | ConvertFrom-Json).repo }
$version = (& $python -c "import biraflow; print(biraflow.__version__)").Trim()
$thumbprint = $SignerThumbprint
if (-not $Unsigned -and -not $thumbprint) {
    $password = Get-Content "packaging\signing\pfx-password.txt" -Raw
    $cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2((Resolve-Path $localPfx), $password)
    $thumbprint = $cert.Thumbprint
}
$updateUrl = if ($Repo) { "https://github.com/$Repo/releases/latest/download/latest.json" } else { "" }
if (-not $Repo) { Write-Warning "Sem -Repo: o app não terá canal de atualização automática." }
if ($Unsigned) { Write-Warning "Build sem assinatura: a atualização automática fica desligada." }

@"
"""Filled in by scripts/build-installer.ps1 for release builds; empty in the source tree.

SIGNER_THUMBPRINT pins the code-signing certificate: an update is installed only if
its installer is signed with exactly this certificate.
"""
SIGNER_THUMBPRINT = "$thumbprint"
UPDATE_URL = "$updateUrl"
"@ | Set-Content "biraflow\_build_info.py" -Encoding utf8

# Windows version resource of the .exe files (Properties > Details)
$v = ($version.Split(".") + @("0", "0", "0"))[0..3] -join ", "
New-Item -ItemType Directory -Force "build" | Out-Null
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($v), prodvers=($v), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
  kids=[StringFileInfo([StringTable('041604B0', [
    StringStruct('CompanyName', 'Bira Flow'), StringStruct('FileDescription', 'Bira Flow - voice dictation'),
    StringStruct('FileVersion', '$version'), StringStruct('ProductName', 'Bira Flow'),
    StringStruct('ProductVersion', '$version'), StringStruct('LegalCopyright', 'Bira Flow')])]),
    VarFileInfo([VarStruct('Translation', [0x0416, 1200])])]
)
"@ | Set-Content "build\version_info.txt" -Encoding utf8

# --- build, sign, package -----------------------------------------------------------------------
Write-Host "Empacotando com o PyInstaller..."
& $python -m PyInstaller --noconfirm --clean --log-level WARN "packaging\biraflow.spec" --distpath dist --workpath "build\pyinstaller"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller falhou" }
$signCmd = ""
if (-not $Unsigned -and -not $SignerThumbprint) {
    foreach ($exe in "dist\BiraFlow\biraflow.exe", "dist\BiraFlow\biraflow-cli.exe") { & "$PSScriptRoot\sign.ps1" -Path $exe }
    $signCmd = 'bfsign=powershell.exe -NoProfile -ExecutionPolicy Bypass -File $q' + "$PSScriptRoot\sign.ps1" + '$q -Path $f'
}

Write-Host "Gerando o instalador com o Inno Setup..."
if ($signCmd) {
    & $iscc /Q "/DAppVersion=$version" "/S$signCmd" "packaging\installer.iss"
} else {
    & $iscc /Q "/DAppVersion=$version" "/DNoSign" "packaging\installer.iss"
}
if ($LASTEXITCODE -ne 0) { throw "Inno Setup falhou" }

$setup = Join-Path $root "dist\BiraFlow-Setup-$version.exe"
$sha = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
$url = if ($Repo) { "https://github.com/$Repo/releases/download/v$version/BiraFlow-Setup-$version.exe" } else { "" }
[ordered]@{ version = $version; url = $url; sha256 = $sha; notes = $Notes } |
    ConvertTo-Json | Set-Content "dist\latest.json" -Encoding utf8

$size = [math]::Round((Get-Item $setup).Length / 1MB, 1)
Write-Host ""
Write-Host "Pronto: $setup ($size MB)"
if ($Unsigned) {
    Write-Host "Assinatura: nenhuma (build sem assinatura)"
} else {
    Write-Host "Assinatura: $((Get-AuthenticodeSignature $setup).Status) · certificado $thumbprint"
}
Write-Host "Publicar a atualização: scripts\publish-release.ps1"
