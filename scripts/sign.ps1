<#
.SYNOPSIS
  Assina um arquivo (.exe) com o certificado do Bira Flow, com carimbo de tempo.
  Usado por build-installer.ps1 e pelo Inno Setup (SignTool).
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
    Write-Error "Falha ao assinar $Path ($($check.Status))"
    exit 1
}
Write-Host ("Assinado: {0} ({1}, carimbo: {2})" -f (Split-Path $Path -Leaf), $check.Status, [bool]$check.TimeStamperCertificate)
