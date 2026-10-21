<#
.SYNOPSIS
  Makes THIS PC trust the Bira Flow certificate (only for your Windows user).
  Run it from Win+R:  powershell -ExecutionPolicy Bypass -File <path>\scripts\trust-signing-cert.ps1

  Windows shows a security warning asking for confirmation: that is it confirming that you
  want to trust this certificate. After that, the installer and the app show up as
  "Publisher: Bira Flow" and the signature is "Valid". On other PCs, run the same script
  with the file packaging\biraflow-codesign.cer (the public certificate only, no key).
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$cer = Join-Path $root "packaging\biraflow-codesign.cer"
if (-not (Test-Path $cer)) { throw "Certificate not found: $cer (run scripts\new-signing-cert.ps1)" }
Import-Certificate -FilePath $cer -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
Import-Certificate -FilePath $cer -CertStoreLocation Cert:\CurrentUser\TrustedPublisher | Out-Null
Write-Host "Done: this Windows user now trusts the installers signed by Bira Flow."
Start-Sleep -Seconds 3
