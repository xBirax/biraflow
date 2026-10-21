<#
.SYNOPSIS
  Cria o certificado de assinatura de código do Bira Flow (gratuito, autoassinado).

  Gera, uma única vez:
    packaging\signing\biraflow-codesign.pfx   chave privada (NÃO compartilhe; fora do git)
    packaging\signing\pfx-password.txt        senha aleatória do .pfx (fora do git)
    packaging\biraflow-codesign.cer           certificado público (pode compartilhar)

  Guarde uma cópia do .pfx e da senha: as atualizações automáticas só aceitam instaladores
  assinados com ESTE certificado. Se ele se perder, será preciso reinstalar manualmente.
#>
param([switch]$Force)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dir = Join-Path $root "packaging\signing"
$pfx = Join-Path $dir "biraflow-codesign.pfx"
$pwdFile = Join-Path $dir "pfx-password.txt"
$cer = Join-Path $root "packaging\biraflow-codesign.cer"
if ((Test-Path $pfx) -and -not $Force) {
    Write-Host "Certificado já existe: $pfx  (use -Force para criar outro; as atualizações deixam de reconhecer o antigo)"
    exit 0
}
New-Item -ItemType Directory -Force $dir | Out-Null

$cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=Bira Flow" `
    -FriendlyName "Bira Flow - assinatura de código" -KeyAlgorithm RSA -KeyLength 3072 `
    -HashAlgorithm SHA256 -KeyExportPolicy Exportable -NotAfter (Get-Date).AddYears(10) `
    -CertStoreLocation Cert:\CurrentUser\My

$bytes = New-Object byte[] 24
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$password = [Convert]::ToBase64String($bytes)
Set-Content -Path $pwdFile -Value $password -NoNewline -Encoding ascii
Export-PfxCertificate -Cert $cert -FilePath $pfx -Password (ConvertTo-SecureString $password -AsPlainText -Force) | Out-Null
Export-Certificate -Cert $cert -FilePath $cer | Out-Null
Remove-Item -Path ("Cert:\CurrentUser\My\" + $cert.Thumbprint)  # the .pfx is the only copy of the key

Write-Host "Certificado criado. Impressão digital: $($cert.Thumbprint)"
Write-Host "Chave privada: $pfx (faça backup dela e de $pwdFile)"
Write-Host "Para o Windows confiar nele neste PC: scripts\trust-signing-cert.ps1 (pelo Win+R)."
