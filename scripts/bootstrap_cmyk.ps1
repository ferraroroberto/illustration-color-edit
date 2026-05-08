#Requires -Version 5.1
<#
.SYNOPSIS
    Bootstraps and validates the CMYK pipeline end-to-end.
    Idempotent — safe to re-run.

    Stage A: install Ghostscript (silent installer, elevated via UAC).
    Stage B: obtain an ICC profile (opens ECI in browser, watches Downloads + profiles/).
    Stage C: sync config.json icc_profile_path to whatever .icc landed in profiles/.
    Stage D: run cmyk-inspect, dry-run, real conversion, and inkcov verification.

.PARAMETER SkipValidation
    Skip stage D. Useful if you only want to provision dependencies.

.PARAMETER IccWaitSeconds
    How long stage B will poll for an ICC drop before giving up. Default 600 (10 min).
#>

[CmdletBinding()]
param(
    [switch]$SkipValidation,
    [int]$IccWaitSeconds = 600
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ProfilesDir = Join-Path $ProjectRoot 'profiles'
$ConfigPath  = Join-Path $ProjectRoot 'config.json'
$Venv        = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$DownloadsDir = Join-Path $env:USERPROFILE 'Downloads'

function Write-Stage([string]$msg) { Write-Host ""; Write-Host "=== $msg ===" -ForegroundColor Cyan }
function Write-Info ([string]$msg) { Write-Host "  $msg" -ForegroundColor Gray }
function Write-Ok   ([string]$msg) { Write-Host "  OK: $msg" -ForegroundColor Green }
function Write-Warn2([string]$msg) { Write-Host "  WARN: $msg" -ForegroundColor Yellow }

# --------- Stage A: Ghostscript ---------------------------------------------
function Get-GsCommand {
    $cmd = Get-Command gswin64c -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = Get-ChildItem 'C:\Program Files\gs\*\bin\gswin64c.exe' -ErrorAction SilentlyContinue |
                  Sort-Object FullName -Descending
    if ($candidates) { return $candidates[0].FullName }
    return $null
}

function Install-Ghostscript {
    Write-Stage 'Stage A: Ghostscript'
    $gs = Get-GsCommand
    if ($gs) {
        $ver = & $gs --version
        Write-Ok "Ghostscript $ver already installed: $gs"
        $gsBin = Split-Path -Parent $gs
        if ($env:PATH -notlike "*$gsBin*") { $env:PATH = "$gsBin;$env:PATH" }
        return $gs
    }

    $url = 'https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs10070/gs10070w64.exe'
    $installer = Join-Path $env:TEMP 'gs10070w64.exe'
    Write-Info "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
    $sizeMB = [Math]::Round((Get-Item $installer).Length / 1MB, 1)
    Write-Ok "Downloaded ($sizeMB MB) -> $installer"

    Write-Info 'Launching installer (silent /S, elevated). UAC prompt will appear on your desktop — approve via RDP.'
    $proc = Start-Process -FilePath $installer -ArgumentList '/S' -Verb RunAs -PassThru -Wait
    if ($proc.ExitCode -ne 0) { throw "Ghostscript installer exited with code $($proc.ExitCode)" }

    $gs = Get-GsCommand
    if (-not $gs) { throw 'Ghostscript installer reported success but gswin64c.exe is not on disk.' }

    $gsBin = Split-Path -Parent $gs
    if ($env:PATH -notlike "*$gsBin*") { $env:PATH = "$gsBin;$env:PATH" }

    $ver = & $gs --version
    Write-Ok "Ghostscript $ver -> $gs"
    return $gs
}

# --------- Stage B: ICC profile ---------------------------------------------
function Find-IccInProfilesDir {
    Get-ChildItem $ProfilesDir -Filter *.icc -ErrorAction SilentlyContinue | Select-Object -First 1
}

function Try-ExtractIccFromZip([string]$zipPath) {
    $extractDir = Join-Path $env:TEMP "icc_extract_$([Guid]::NewGuid().ToString('N'))"
    try {
        Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force
        $iccFiles = Get-ChildItem $extractDir -Recurse -Include *.icc, *.icm -ErrorAction SilentlyContinue
        if (-not $iccFiles) { return $null }
        foreach ($f in $iccFiles) {
            Copy-Item $f.FullName -Destination $ProfilesDir -Force
            Write-Ok "Installed: $($f.Name)"
        }
        return (Find-IccInProfilesDir)
    } finally {
        if (Test-Path $extractDir) { Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

function Ensure-IccProfile {
    Write-Stage 'Stage B: ICC profile'

    $existing = Find-IccInProfilesDir
    if ($existing) {
        Write-Ok "ICC profile already present: $($existing.Name)"
        return $existing.FullName
    }

    # Pre-existing zip in Downloads? Extract immediately without opening browser.
    $preZip = Get-ChildItem $DownloadsDir -Filter '*.zip' -ErrorAction SilentlyContinue |
              Where-Object { $_.Name -match '(?i)eci|icc|iso.?coated|pso' } |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($preZip) {
        Write-Info "Found existing zip in Downloads: $($preZip.Name)"
        $picked = Try-ExtractIccFromZip $preZip.FullName
        if ($picked) { return $picked.FullName }
    }

    Write-Info 'Opening ECI downloads page in your default browser.'
    Write-Info 'Pick one ICC profile package and click Download:'
    Write-Info '  * eciCMYK (recommended, modern)'
    Write-Info '  * PSO Coated v3 / PSO Uncoated v3'
    Write-Info '  * (older site sections may still link ISO Coated v2 / ECI Offset 2009)'
    Write-Info "Either let it land in $DownloadsDir, or drop the .icc directly into $ProfilesDir."
    Start-Process 'https://www.eci.org/en/downloads'

    $deadline = (Get-Date).AddSeconds($IccWaitSeconds)
    Write-Info "Watching for an ICC file (timeout: ${IccWaitSeconds}s; Ctrl-C to abort and re-run later)..."
    while ((Get-Date) -lt $deadline) {
        $direct = Find-IccInProfilesDir
        if ($direct) {
            Write-Ok "Detected ICC drop: $($direct.Name)"
            return $direct.FullName
        }

        $zip = Get-ChildItem $DownloadsDir -Filter '*.zip' -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -match '(?i)eci|icc|iso.?coated|pso' } |
               Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($zip) {
            Write-Info "Found candidate zip: $($zip.Name)"
            $picked = Try-ExtractIccFromZip $zip.FullName
            if ($picked) { return $picked.FullName }
            Write-Warn2 "Zip had no .icc/.icm; will keep watching."
        }

        # Also accept .icc dropped directly into Downloads
        $directDl = Get-ChildItem $DownloadsDir -Include *.icc, *.icm -ErrorAction SilentlyContinue |
                    Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($directDl) {
            Copy-Item $directDl.FullName -Destination $ProfilesDir -Force
            Write-Ok "Copied $($directDl.Name) from Downloads -> profiles/"
            return (Find-IccInProfilesDir).FullName
        }

        Start-Sleep -Seconds 3
    }
    throw "Timed out waiting for an ICC profile. Drop one into '$ProfilesDir' and re-run the script."
}

# --------- Stage C: align config.json to actual ICC + GS paths -------------
function Sync-Config([string]$IccFullPath, [string]$GsFullPath) {
    Write-Stage 'Stage C: config.json sync'
    $iccName = Split-Path -Leaf $IccFullPath
    $iccRel  = "./profiles/$iccName"

    $cfg = Get-Content $ConfigPath -Raw | ConvertFrom-Json
    $changed = $false

    if ($cfg.cmyk_export.icc_profile_path -ne $iccRel) {
        Write-Info "icc_profile_path: $($cfg.cmyk_export.icc_profile_path) -> $iccRel"
        $cfg.cmyk_export.icc_profile_path = $iccRel
        $changed = $true
    }
    if ($cfg.cmyk_export.ghostscript_path -ne $GsFullPath) {
        Write-Info "ghostscript_path: $($cfg.cmyk_export.ghostscript_path) -> $GsFullPath"
        $cfg.cmyk_export.ghostscript_path = $GsFullPath
        $changed = $true
    }

    if (-not $changed) {
        Write-Ok 'config.json already in sync'
        return
    }
    $json = $cfg | ConvertTo-Json -Depth 10
    # Write UTF-8 *without* BOM — PowerShell 5.1's Set-Content -Encoding UTF8 emits BOM
    # which Python's json module rejects.
    [System.IO.File]::WriteAllText($ConfigPath, $json, (New-Object System.Text.UTF8Encoding $false))
    Write-Ok 'config.json updated'
}

# --------- Stage D: validation ----------------------------------------------
function Invoke-CmykValidation {
    Write-Stage 'Stage D: validation'
    if (-not (Test-Path $Venv)) { throw "Venv python not found at $Venv" }
    $svg = Get-ChildItem (Join-Path $ProjectRoot 'input') -Filter *.svg | Select-Object -First 1
    if (-not $svg) { throw 'No SVGs found in input/.' }
    Write-Info "Test SVG: $($svg.Name)"

    Push-Location $ProjectRoot
    try {
        Write-Info '[1/4] cmyk-inspect'
        & $Venv -m src.cli cmyk-inspect $svg.FullName --show-command
        if ($LASTEXITCODE -ne 0) { throw "cmyk-inspect failed (exit $LASTEXITCODE)" }

        Write-Info '[2/4] cmyk-convert --dry-run'
        & $Venv -m src.cli cmyk-convert --dry-run
        if ($LASTEXITCODE -ne 0) { throw "cmyk-convert --dry-run failed (exit $LASTEXITCODE)" }

        Write-Info '[3/4] cmyk-convert (real)'
        & $Venv -m src.cli cmyk-convert
        if ($LASTEXITCODE -ne 0) { throw "cmyk-convert failed (exit $LASTEXITCODE)" }

        $pdf = Get-ChildItem (Join-Path $ProjectRoot 'output_cmyk') -Filter '*_CMYK.pdf' -ErrorAction SilentlyContinue |
               Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($pdf) {
            Write-Info "[4/4] gswin64c inkcov $($pdf.Name)"
            $gs = Get-GsCommand
            # PowerShell's 5.1 native-cmd pipeline swallows GS stdout; tee to a temp file
            # to surface the per-channel coverage numbers.
            $covLog = Join-Path $env:TEMP "inkcov_$([Guid]::NewGuid().ToString('N')).txt"
            & $gs -dQUIET -dNOPAUSE -dBATCH -sDEVICE=inkcov "-sOutputFile=-" $pdf.FullName *> $covLog
            $cov = (Get-Content $covLog -Raw).Trim()
            Remove-Item $covLog -ErrorAction SilentlyContinue
            if ($cov) {
                Write-Ok "Ink coverage (C M Y K): $cov"
            } else {
                Write-Warn2 'inkcov returned no output.'
            }
        } else {
            Write-Warn2 'No CMYK PDF produced — skipping inkcov.'
        }
    } finally {
        Pop-Location
    }
    Write-Ok 'Validation complete. QA report: output_cmyk\cmyk_qa_report.html'
}

# --------- Main -------------------------------------------------------------
$gs  = Install-Ghostscript
$icc = Ensure-IccProfile
Sync-Config $icc $gs
if (-not $SkipValidation) { Invoke-CmykValidation }
