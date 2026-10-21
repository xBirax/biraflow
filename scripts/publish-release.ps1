<#
.SYNOPSIS
  Publishes the installer built by build-installer.ps1 on the GitHub Releases. The installed
  apps find the new version within 6 hours (or in "Check now") and update themselves.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\publish-release.ps1 -Notes "Fixes and improvements"
#>
param([string]$Notes = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$repo = (Get-Content "packaging\release.json" -Raw | ConvertFrom-Json).repo
$manifest = Get-Content "dist\latest.json" -Raw | ConvertFrom-Json
$version = $manifest.version
$setup = "dist\BiraFlow-Setup-$version.exe"
if (-not (Test-Path $setup)) { throw "Installer not found: $setup (run build-installer.ps1)" }
if ((Get-FileHash $setup -Algorithm SHA256).Hash.ToLower() -ne $manifest.sha256) { throw "latest.json does not match $setup" }
if (-not $Notes) { $Notes = if ($manifest.notes) { $manifest.notes } else { "Bira Flow $version" } }

gh release create "v$version" $setup "dist\latest.json" --repo $repo --title "Bira Flow $version" --notes $Notes
if ($LASTEXITCODE -ne 0) { throw "gh release create failed" }
Write-Host "Published: https://github.com/$repo/releases/tag/v$version"
