<#
.SYNOPSIS
  Creates the Bira Flow code-signing certificate (free, self-signed).

  Generates, once:
    packaging\signing\biraflow-codesign.pfx   the private key (DO NOT share it; outside git)
    packaging\signing\pfx-password.txt        a random password for the .pfx (outside git)
    packaging\biraflow-codesign.cer           the public certificate (safe to share)

  Keep a copy of the .pfx and of the password: the automatic updates only accept installers
  signed with THIS certificate. If it is lost, users have to install by hand.
#>
param([switch]$Force)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dir = Join-Path $root "packaging\signing"
$pfx = Join-Path $dir "biraflow-codesign.pfx"
$pwdFile = Join-Path $dir "pfx-password.txt"
$cer = Join-Path $root "packaging\biraflow-codesign.cer"
if ((Test-Path $pfx) -and -not $Force) {
    Write-Host "Certificate already exists: $pfx  (use -Force to create another one; the updates stop recognising the old)"
    exit 0
}
New-Item -ItemType Directory -Force $dir | Out-Null

$cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=Bira Flow" `
    -FriendlyName "Bira Flow - code signing" -KeyAlgorithm RSA -KeyLength 3072 `
    -HashAlgorithm SHA256 -KeyExportPolicy Exportable -NotAfter (Get-Date).AddYears(10) `
    -CertStoreLocation Cert:\CurrentUser\My

$bytes = New-Object byte[] 24
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$password = [Convert]::ToBase64String($bytes)
Set-Content -Path $pwdFile -Value $password -NoNewline -Encoding ascii
Export-PfxCertificate -Cert $cert -FilePath $pfx -Password (ConvertTo-SecureString $password -AsPlainText -Force) | Out-Null
Export-Certificate -Cert $cert -FilePath $cer | Out-Null
Remove-Item -Path ("Cert:\CurrentUser\My\" + $cert.Thumbprint)  # the .pfx is the only copy of the key

Write-Host "Certificate created. Thumbprint: $($cert.Thumbprint)"
Write-Host "Private key: $pfx (back it up, and $pwdFile too)"
Write-Host "For Windows to trust it on this PC: scripts\trust-signing-cert.ps1 (through Win+R)."

