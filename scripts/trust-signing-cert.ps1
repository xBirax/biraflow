<#
.SYNOPSIS
  Faz ESTE PC confiar no certificado do Bira Flow (só no seu usuário do Windows).
  Rode pelo Win+R:  powershell -ExecutionPolicy Bypass -File F:\Whisperflow\scripts\trust-signing-cert.ps1

  O Windows mostra um aviso de segurança pedindo confirmação: é ele confirmando que você
  quer confiar neste certificado. Depois disso, o instalador e o app aparecem como
  "Editor: Bira Flow" e a assinatura fica "Válida". Em outros PCs, rode o mesmo script com
  o arquivo packaging\biraflow-codesign.cer (só o certificado público, sem a chave).
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$cer = Join-Path $root "packaging\biraflow-codesign.cer"
if (-not (Test-Path $cer)) { throw "Certificado não encontrado: $cer (rode scripts\new-signing-cert.ps1)" }
Import-Certificate -FilePath $cer -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
Import-Certificate -FilePath $cer -CertStoreLocation Cert:\CurrentUser\TrustedPublisher | Out-Null
Write-Host "Pronto: este usuário do Windows agora confia nos instaladores assinados pelo Bira Flow."
Start-Sleep -Seconds 3
