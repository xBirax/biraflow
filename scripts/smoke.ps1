<#
Runs every check of docs/PLANO-REUNIOES.md section 11.4 in one go and writes a report:
  1. synthetic fixtures (scripts\make-test-audio.ps1) when missing
  2. pytest -q (unit tests)
  3. pytest -m integration -s (real engines; tests needing a missing key or model are skipped)
  4. biraflow --selftest
Report: %TEMP%\biraflow-smoke\relatorio.md (pass/fail per step + the measurements the
integration tests print: time, anchors, speakers, timing). Never part of the installer.
Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke.ps1
#>
param([string]$OutDir = (Join-Path $env:TEMP 'biraflow-smoke'))
$ErrorActionPreference = 'Stop'
# Keep this file ASCII: Windows PowerShell 5.1 reads BOM-less scripts as ANSI.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root '.venv\Scripts\python.exe'
New-Item -ItemType Directory -Force $OutDir | Out-Null
$fixtures = if ($env:BIRAFLOW_FIXTURES) { $env:BIRAFLOW_FIXTURES } else { Join-Path $env:TEMP 'biraflow-fixtures' }
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [Text.Encoding]::UTF8   # read Python's UTF-8 output as UTF-8
$steps = @()

function Step([string]$name, [scriptblock]$run) {
    $log = Join-Path $OutDir (($name -replace '[^a-z0-9]+', '-') + '.log')
    $t0 = Get-Date
    # 'Stop' would turn any stderr line of a native command into a terminating error (PS 5.1)
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    try { & $run *> $log } catch { $_ | Out-File -Append $log; $global:LASTEXITCODE = 1 }
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = 'Stop'
    $secs = [int]((Get-Date) - $t0).TotalSeconds
    Write-Host ("{0,-28} {1} ({2} s)" -f $name, $(if ($ok) { 'ok' } else { 'FALHOU' }), $secs)
    $script:steps += [pscustomobject]@{ Name = $name; Ok = $ok; Seconds = $secs; Log = $log }
}

if (-not (Test-Path (Join-Path $fixtures 'reuniao-2vozes.wav'))) {
    Step 'fixtures' { powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make-test-audio.ps1 -OutDir $fixtures }
}
$env:BIRAFLOW_FIXTURES = $fixtures
Step 'pytest' { & $python -m pytest -q }
Step 'pytest integration' { & $python -m pytest -m integration -s -rs -q }
Step 'selftest' { & $python -m biraflow --selftest }

$version = (& $python -c "import biraflow; print(biraflow.__version__)").Trim()
$lines = @("# Bira Flow $version - smoke tests", "", "Gerado em $(Get-Date -Format 'yyyy-MM-dd HH:mm').", "",
           "| Etapa | Resultado | Tempo |", "|---|---|---|")
foreach ($s in $steps) { $lines += "| $($s.Name) | $(if ($s.Ok) { 'ok' } else { '**falhou**' }) | $($s.Seconds) s |" }
$integration = $steps | Where-Object { $_.Name -eq 'pytest integration' } | Select-Object -First 1
if ($integration) {
    $lines += '', '## Medidas', '', '```'
    # the lines the integration tests print (Portuguese, one per measurement) and the skip reasons
    $lines += Get-Content $integration.Log -Encoding UTF8 | Where-Object { $_ -match '(RTF|falantes|recuperada|notas via|sem internet|deriva|SKIPPED|passed|failed)' }
    $lines += '```'
}
$report = Join-Path $OutDir 'relatorio.md'
$lines | Set-Content -Encoding UTF8 $report
Write-Host "Relatorio: $report"
if ($steps | Where-Object { -not $_.Ok }) { exit 1 }
