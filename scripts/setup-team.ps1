<#
.SYNOPSIS
  Deixa um PC da equipe com o Bira Flow 100% em nuvem: transcrição pela Groq e limpeza pelo
  Vercel AI Gateway, sem baixar o modelo local (500 MB a 1,5 GB, e pesado num computador fraco).

.DESCRIPTION
  Nesta ordem:
    1. instala ("instala" = põe em %LOCALAPPDATA%\Programs\Bira Flow), de duas formas:
         - pasta portátil: -SourceDir aponta para a pasta descompactada do Bira Flow (o zip que
           você distribui). Não usa instalador nem certificado, e o Windows não avisa nada;
         - instalador do Inno: -Installer aponta para o BiraFlow-Setup-<versão>.exe (silencioso);
    2. grava as chaves em %APPDATA%\BiraFlow\.env (copiando um .env pronto ou as duas chaves);
    3. ajusta %APPDATA%\BiraFlow\settings.toml: engine e cleanup.provider;
    4. confere com "biraflow-cli.exe --check" e resume o que ficou.

  Rodar de novo é seguro: os mesmos valores são reescritos, sem duplicar linhas, e o app é
  atualizado por cima (ele é fechado antes). Se a variável BIRAFLOW_HOME existir (como neste PC de
  desenvolvimento), a pasta de dados é ela.

.PARAMETER SourceDir
  Pasta do app descompactada (contém biraflow.exe e _internal). É o caminho da equipe: sem
  instalador, sem assinatura, sem aviso do Windows.

.PARAMETER Installer
  Caminho do BiraFlow-Setup-<versão>.exe. Sem ele, o script só configura (ou copia a pasta).

.PARAMETER Autostart
  Com -SourceDir: põe o app para iniciar junto com o Windows (o atalho é criado sempre).

.PARAMETER EnvFile
  Um .env já preparado, copiado por cima do que existir. É a forma recomendada: as chaves não
  aparecem no histórico do PowerShell nem na linha de comando.

.PARAMETER GroqKey
  Chave da Groq (transcrição). Use com -GatewayKey, ou prefira -EnvFile.

.PARAMETER GatewayKey
  Chave do Vercel AI Gateway: serve para a limpeza do texto, as notas da reunião, a leitura de
  print e (com -Engine gateway, ou sozinha no .env com -Engine auto) também a transcrição.

.PARAMETER Engine
  Motor da transcrição: "auto" (padrão: a primeira chave que existir, Groq antes do Gateway),
  "groq" ou "gateway". Com a chave só do Gateway, "auto" já transcreve por lá.

.PARAMETER AppDir
  Onde está o biraflow-cli.exe. Padrão: %LOCALAPPDATA%\Programs\Bira Flow (onde o instalador põe).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup-team.ps1 -Installer .\BiraFlow-Setup-1.1.0.exe -EnvFile \\servidor\bira\.env

.EXAMPLE
  # EQUIPE: pasta portátil (o zip que você distribui), chaves de um .env e atalho no Menu Iniciar
  powershell -ExecutionPolicy Bypass -File scripts\setup-team.ps1 -SourceDir \\servidor\bira\BiraFlow -EnvFile \\servidor\bira\.env -Autostart

.EXAMPLE
  # com o instalador do Inno Setup
  powershell -ExecutionPolicy Bypass -File scripts\setup-team.ps1 -Installer .\BiraFlow-Setup-1.1.0.exe -EnvFile \\servidor\bira\.env

.EXAMPLE
  # já instalado: só configura e confere
  powershell -ExecutionPolicy Bypass -File scripts\setup-team.ps1 -EnvFile .\.env -NoInstall

.EXAMPLE
  # chaves direto na linha de comando (ficam no histórico desta sessão)
  powershell -ExecutionPolicy Bypass -File scripts\setup-team.ps1 -GroqKey gsk_xxx -GatewayKey vck_yyy
#>
param(
    [string]$SourceDir,
    [string]$Installer,
    [string]$EnvFile,
    [string]$GroqKey,
    [string]$GatewayKey,
    [ValidateSet("vercel", "auto")][string]$Cleanup = "vercel",
    [ValidateSet("auto", "groq", "gateway")][string]$Engine = "auto",
    [string]$AppDir,
    [switch]$Autostart,
    [switch]$NoShortcut,
    [switch]$NoInstall
)
$ErrorActionPreference = "Stop"

function Passo([string]$texto) { Write-Host ""; Write-Host "== $texto" -ForegroundColor Cyan }
function Recado([string]$texto) { Write-Host "  $texto" }

$data = if ($env:BIRAFLOW_HOME) { $env:BIRAFLOW_HOME } else { Join-Path $env:APPDATA "BiraFlow" }
$appDir = if ($AppDir) { $AppDir } else { Join-Path $env:LOCALAPPDATA "Programs\Bira Flow" }
$cli = Join-Path $appDir "biraflow-cli.exe"

Passo "PC da equipe: Bira Flow em nuvem (Groq + Vercel AI Gateway)"
Recado "pasta de dados : $data"
Recado "pasta do app   : $appDir"

# --- 1. instalar ------------------------------------------------------------------------------
if (-not $NoInstall -and $SourceDir) {
    # pasta portátil: copia o app para $appDir (e atualiza por cima, se já estava lá)
    Passo "Copiando o app (pasta portátil, sem instalador)"
    if (-not (Test-Path (Join-Path $SourceDir "biraflow.exe"))) { throw "Não achei biraflow.exe em $SourceDir." }
    Get-Process biraflow, biraflow-cli -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($appDir, "OrdinalIgnoreCase") } |
        ForEach-Object { Recado "fechando $($_.ProcessName) ($($_.Id))"; Stop-Process -Id $_.Id -Force }
    Start-Sleep -Milliseconds 300
    New-Item -ItemType Directory -Force -Path $appDir | Out-Null
    Remove-Item (Join-Path $appDir "_internal") -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item -Path (Join-Path $SourceDir "*") -Destination $appDir -Recurse -Force
    Recado "copiado de $SourceDir"
    if (-not $NoShortcut) {
        $lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "Bira Flow.lnk"
        $shell = New-Object -ComObject WScript.Shell
        $atalho = $shell.CreateShortcut($lnk)
        $atalho.TargetPath = Join-Path $appDir "biraflow.exe"
        $atalho.WorkingDirectory = $appDir
        $atalho.Description = "Ditado por voz em qualquer aplicativo"
        $atalho.IconLocation = (Join-Path $appDir "biraflow.exe") + ",0"
        $atalho.Save()
        Recado "atalho no Menu Iniciar: $lnk"
    }
    if ($Autostart) {
        $run = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
        New-Item -Path $run -Force | Out-Null
        Set-ItemProperty -Path $run -Name "BiraFlow" -Value ('"' + (Join-Path $appDir "biraflow.exe") + '"')
        Recado "inicia com o Windows: sim"
    } else {
        Recado "inicia com o Windows: não (use -Autostart para ligar)"
    }
} elseif (-not $NoInstall -and $Installer) {
    Passo "Instalando (silencioso, sem administrador)"
    if (-not (Test-Path $Installer)) { throw "Instalador não encontrado: $Installer" }
    # /CLOSEAPPLICATIONS + /FORCECLOSEAPPLICATIONS: o app aberto segura os arquivos que o
    # instalador precisa trocar (sem isso a atualização silenciosa falha sem avisar);
    # /RELAUNCH=1 reabre o app no fim, como o atualizador de dentro do app faz.
    $p = Start-Process -FilePath $Installer -Wait -PassThru -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES",
        "/NORESTART", "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS", "/RELAUNCH=1"
    if ($p.ExitCode -ne 0) { throw "O instalador terminou com código $($p.ExitCode)." }
    Recado "instalado em $appDir"
} else {
    Recado "instalação: pulada (só configuração)"
}
if (-not (Test-Path $cli)) { throw "biraflow-cli.exe não encontrado em $appDir. Passe -SourceDir (pasta portátil) ou -Installer (BiraFlow-Setup.exe)." }

# --- 2. chaves ---------------------------------------------------------------------------------
Passo "Chaves (.env)"
New-Item -ItemType Directory -Force -Path $data | Out-Null
$envTarget = Join-Path $data ".env"
if ($EnvFile) {
    if (-not (Test-Path $EnvFile)) { throw ".env não encontrado: $EnvFile" }
    $utf8SemBom = New-Object System.Text.UTF8Encoding($false)
    $novas = @{}
    foreach ($linha in [System.IO.File]::ReadAllLines($EnvFile)) {
        if ($linha -match "^\s*([A-Z_0-9]+)\s*=\s*(\S.*)$") { $novas[$matches[1]] = $matches[2].Trim() }
    }
    $antigas = @{}
    if (Test-Path $envTarget) {
        Copy-Item -LiteralPath $envTarget -Destination "$envTarget.bak" -Force
        Recado "chaves anteriores guardadas em $envTarget.bak"
        foreach ($linha in [System.IO.File]::ReadAllLines($envTarget)) {
            if ($linha -match "^\s*([A-Z_0-9]+)\s*=\s*(\S.*)$") { $antigas[$matches[1]] = $matches[2].Trim() }
        }
    }
    # o arquivo novo manda; o que só existia no antigo é mantido (não perder um acesso já configurado)
    $linhas = @([System.IO.File]::ReadAllLines($EnvFile))
    $mantidas = @($antigas.Keys | Where-Object { -not $novas.ContainsKey($_) -and $antigas[$_] })
    foreach ($nome in $mantidas) { $linhas += "$nome=$($antigas[$nome])" }
    [System.IO.File]::WriteAllLines($envTarget, $linhas, $utf8SemBom)
    Recado "copiado de $EnvFile"
    if ($mantidas.Count) { Recado ("mantidas do .env anterior: " + ($mantidas -join ", ")) }
} elseif ($GroqKey -or $GatewayKey) {
    $linhas = if (Test-Path $envTarget) { [System.IO.File]::ReadAllLines($envTarget) } else { @() }
    foreach ($par in @(@("GROQ_API_KEY", $GroqKey), @("AI_GATEWAY_API_KEY", $GatewayKey))) {
        $nome, $valor = $par
        if (-not $valor) { continue }
        $achou = $false
        for ($i = 0; $i -lt $linhas.Count; $i++) {
            if ($linhas[$i] -match "^\s*$nome\s*=") { $linhas[$i] = "$nome=$valor"; $achou = $true }
        }
        if (-not $achou) { $linhas += "$nome=$valor" }
        Recado "$nome gravada"
    }
    [System.IO.File]::WriteAllLines($envTarget, $linhas, (New-Object System.Text.UTF8Encoding($false)))
} else {
    if (Test-Path $envTarget) {
        Recado "mantidas as chaves que já estavam em $envTarget"
    } else {
        throw "Sem chaves: passe -EnvFile, ou -GroqKey/-GatewayKey."
    }
}

# --- 3. settings.toml --------------------------------------------------------------------------
Passo "Configuração (settings.toml)"
$settingsPath = Join-Path $data "settings.toml"
$utf8 = New-Object System.Text.UTF8Encoding($false)
if (-not (Test-Path $settingsPath)) {
    # arquivo mínimo: o app completa o resto com os padrões ao ler
    $texto = "[transcription]`r`nengine = `"$Engine`"`r`n`r`n[cleanup]`r`nprovider = `"$Cleanup`"`r`n"
    [System.IO.File]::WriteAllText($settingsPath, $texto, $utf8)
    Recado "criado com engine = `"$Engine`" e provider = `"$Cleanup`""
} else {
    $linhas = [System.IO.File]::ReadAllLines($settingsPath)
    $secao = ""
    for ($i = 0; $i -lt $linhas.Count; $i++) {
        if ($linhas[$i] -match "^\s*\[([^\]]+)\]") { $secao = $matches[1].ToLower() }
        elseif ($secao -eq "transcription" -and $linhas[$i] -match "^\s*engine\s*=") { $linhas[$i] = "engine = `"$Engine`"" }
        elseif ($secao -eq "cleanup" -and $linhas[$i] -match "^\s*provider\s*=") { $linhas[$i] = "provider = `"$Cleanup`"" }
    }
    [System.IO.File]::WriteAllLines($settingsPath, $linhas, $utf8)
    Recado "ajustado: engine = `"$Engine`", provider = `"$Cleanup`" (o resto do arquivo ficou como estava)"
}
Recado "modelo local: não é baixado nem usado (nada de whisper.cpp, nada de GPU)"

# --- 4. conferir -------------------------------------------------------------------------------
Passo "Conferindo (biraflow-cli --check)"
$saida = & $cli --check 2>&1 | Out-String
Write-Host $saida

$problemas = @()
$esperadas = @()
foreach ($nome in @("GROQ_API_KEY", "AI_GATEWAY_API_KEY")) {
    $achou = [System.IO.File]::ReadAllLines($envTarget) | Where-Object { $_ -match "^\s*$nome\s*=\s*\S" }
    if ($achou) { $esperadas += $nome }
    if ($achou -and $saida -notmatch "$nome\s*:\s*definida") { $problemas += "$nome não foi lida pelo app" }
}
if ($esperadas.Count -eq 0) { $problemas += "nenhuma chave no .env: sem chave não há transcrição" }
if ($Engine -eq "gateway" -and $esperadas -notcontains "AI_GATEWAY_API_KEY") {
    $problemas += "engine = gateway sem AI_GATEWAY_API_KEY no .env"
}
$config = [System.IO.File]::ReadAllText($settingsPath)
if ($config -notmatch "engine\s*=\s*`"$Engine`"") { $problemas += "settings.toml não ficou com engine = `"$Engine`"" }
if ($config -notmatch "provider\s*=\s*`"$Cleanup`"") { $problemas += "settings.toml não ficou com provider = `"$Cleanup`"" }

Passo "Resumo"
$motor = if ($Engine -eq "gateway" -or ($Engine -eq "auto" -and $esperadas -notcontains "GROQ_API_KEY")) {
    "pelo Vercel AI Gateway"
} else { "pela Groq" }
if ($problemas.Count -eq 0) {
    Write-Host "  Pronto: este PC transcreve $motor ($($esperadas -join ', ')) e limpa pelo Gateway." -ForegroundColor Green
} else {
    foreach ($p in $problemas) { Write-Host "  ! $p" -ForegroundColor Yellow }
}
Recado "sem internet: o ditado fica guardado em $data\falhas e volta pelo menu da bandeja,"
Recado "              em 'Transcrever de novo o último áudio' (o modelo local é opcional)."
if ($motor -eq "pelo Vercel AI Gateway") {
    Recado "privacidade: o áudio do ditado e da reunião sobe para o Vercel AI Gateway."
} else {
    Recado "privacidade: o áudio do ditado sobe para a Groq; o texto, para o Vercel AI Gateway."
}
Recado "a reunião pede consentimento antes de gravar e sobe em pedaços de 90 s."
