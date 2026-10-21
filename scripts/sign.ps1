<#
.SYNOPSIS
  Signs a file (.exe) with the Bira Flow certificate, with a timestamp.
  Used by build-installer.ps1 and by Inno Setup (SignTool).
#>
param([Parameter(Mandatory)][string]$Path)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pfx = Join-Path $root "packaging\signing\biraflow-codesign.pfx"
$password = Get-Content (Join-Path $root "packaging\signing\pfx-password.txt") -Raw
$flags = [Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable
$cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2($pfx, $password, $flags)

# The timestamp keeps the signature valid after the certificate expires; try a few free servers.
foreach ($ts in "http://timestamp.digicert.com", "http://timestamp.sectigo.com", "http://time.certum.pl") {
    $sig = Set-AuthenticodeSignature -FilePath $Path -Certificate $cert -HashAlgorithm SHA256 -TimestampServer $ts
    if ($sig.SignerCertificate -and $sig.Status -ne "HashMismatch" -and $sig.TimeStamperCertificate) { break }
}
$check = Get-AuthenticodeSignature -FilePath $Path
if (-not $check.SignerCertificate -or $check.SignerCertificate.Thumbprint -ne $cert.Thumbprint) {
    Write-Error "Could not sign $Path ($($check.Status))"
    exit 1
}
Write-Host ("Signed: {0} ({1}, timestamp: {2})" -f (Split-Path $Path -Leaf), $check.Status, [bool]$check.TimeStamperCertificate)
