<#
.SYNOPSIS
  Publica o instalador gerado por build-installer.ps1 nos Releases do GitHub. Os apps
  instalados encontram a nova versão em até 6 horas (ou em "Verificar agora") e se atualizam.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\publish-release.ps1 -Notes "Correções e melhorias"
#>
param([string]$Notes = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$repo = (Get-Content "packaging\release.json" -Raw | ConvertFrom-Json).repo
$manifest = Get-Content "dist\latest.json" -Raw | ConvertFrom-Json
$version = $manifest.version
$setup = "dist\BiraFlow-Setup-$version.exe"
if (-not (Test-Path $setup)) { throw "Instalador não encontrado: $setup (rode build-installer.ps1)" }
if ((Get-FileHash $setup -Algorithm SHA256).Hash.ToLower() -ne $manifest.sha256) { throw "latest.json não confere com $setup" }
if (-not $Notes) { $Notes = if ($manifest.notes) { $manifest.notes } else { "Bira Flow $version" } }

gh release create "v$version" $setup "dist\latest.json" --repo $repo --title "Bira Flow $version" --notes $Notes
if ($LASTEXITCODE -ne 0) { throw "gh release create falhou" }
Write-Host "Publicado: https://github.com/$repo/releases/tag/v$version"
