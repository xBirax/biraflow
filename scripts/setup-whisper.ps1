<#
.SYNOPSIS
  Baixa o whisper.cpp (whisper-cli.exe) e um modelo para %LOCALAPPDATA%\BiraFlow.
.PARAMETER Model
  tiny | base | small | medium | large-v3-turbo (ou variantes quantizadas, ex.: small-q5_1)
.PARAMETER Build
  cpu (padrão) | blas (OpenBLAS, mais rápido em CPUs modernas) | cuda12 | cuda11 (placas NVIDIA)
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup-whisper.ps1 -Model medium -Build blas
#>
param(
    [string]$Model = "small",
    [ValidateSet("cpu", "blas", "cuda12", "cuda11")] [string]$Build = "cpu",
    [switch]$Force
)
$ErrorActionPreference = "Stop"
$home_ = if ($env:BIRAFLOW_HOME) { $env:BIRAFLOW_HOME } else { Join-Path $env:LOCALAPPDATA "BiraFlow" }
$bin = Join-Path $home_ "whisper"
$models = Join-Path $home_ "models"
New-Item -ItemType Directory -Force $bin, $models | Out-Null

# --- whisper.cpp ---------------------------------------------------------------
$cli = Join-Path $bin "whisper-cli.exe"
if ((Test-Path $cli) -and -not $Force) {
    Write-Host "whisper-cli.exe is already installed ($cli). Use -Force to install it again."
} else {
    $pattern = switch ($Build) {
        "cpu"    { '^whisper-bin-x64\.zip$' }
        "blas"   { '^whisper-blas-bin-x64\.zip$' }
        "cuda12" { '(cublas|cuda)-12[\d.]*-?(bin-)?x64\.zip$' }
        "cuda11" { '(cublas|cuda)-11[\d.]*-?(bin-)?x64\.zip$' }
    }
    Write-Host "Looking for the '$Build' build in the whisper.cpp releases..."
    $releases = Invoke-RestMethod "https://api.github.com/repos/ggml-org/whisper.cpp/releases?per_page=20" `
        -Headers @{ "User-Agent" = "biraflow-setup" }
    $asset = $null
    foreach ($r in $releases) {
        $asset = $r.assets | Where-Object { $_.name -match $pattern } | Select-Object -First 1
        if ($asset) { Write-Host "Release $($r.tag_name): $($asset.name) ($([math]::Round($asset.size / 1MB, 1)) MB)"; break }
    }
    if (-not $asset) { throw "No recent release has a '$pattern' file. Download it by hand at https://github.com/ggml-org/whisper.cpp/releases" }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("biraflow-" + [guid]::NewGuid())
    New-Item -ItemType Directory $tmp | Out-Null
    try {
        $zip = Join-Path $tmp $asset.name
        curl.exe -L --fail --progress-bar -o $zip $asset.browser_download_url
        if ($LASTEXITCODE -ne 0) { throw "The download of $($asset.name) failed." }
        Expand-Archive $zip -DestinationPath (Join-Path $tmp "x") -Force
        $found = Get-ChildItem (Join-Path $tmp "x") -Recurse -Filter "whisper-cli.exe" | Select-Object -First 1
        if (-not $found) { throw "whisper-cli.exe is not inside $($asset.name)." }
        Copy-Item (Join-Path $found.DirectoryName "*") $bin -Recurse -Force
        Write-Host "whisper.cpp installed in $bin"
    } finally {
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# --- model -------------------------------------------------------------------------
$file = Join-Path $models "ggml-$Model.bin"
if ((Test-Path $file) -and -not $Force) {
    Write-Host "Model already downloaded: $file"
} else {
    $url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$Model.bin"
    Write-Host "Downloading the '$Model' model (small ~470 MB, medium ~1.5 GB)..."
    curl.exe -L --fail --progress-bar -o "$file.part" $url
    if ($LASTEXITCODE -ne 0) { Remove-Item "$file.part" -ErrorAction SilentlyContinue; throw "The download of $url failed" }
    Move-Item "$file.part" $file -Force
    Write-Host "Model saved in $file"
}

Write-Host ""
if ($Model -ne "small") {
    Write-Host "Remember to set in settings.toml:  local_model = `"$Model`""
}
Write-Host "Test:  .venv\Scripts\python -m biraflow --check"

