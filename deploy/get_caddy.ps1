# =============================================================================
# get_caddy.ps1 - Download the Caddy web server binary onto the SDH server.
# =============================================================================
# NON-ADMIN: this only downloads + extracts a single caddy.exe into
# deploy\caddy\. It does NOT bind any port, install a service, or touch HTTP.sys,
# so it needs no elevation. Freeing port 80 + starting the proxy are the
# elevated steps in deploy\CADDY_RUNBOOK.md.
#
# Usage (on the server, from any shell):
#   powershell -ExecutionPolicy Bypass -File C:\projects\service_desk_helper\deploy\get_caddy.ps1
#
# Pinned to a known-good release with a SHA-256 integrity check. Bump $Version
# and $Sha256 together to upgrade (get the hash from the release's checksums).
# =============================================================================
[CmdletBinding()]
param(
    [string]$Version = "2.11.4",
    # SHA-256 of caddy_2.11.4_windows_amd64.zip (verified from the GitHub release
    # asset digest). If cleared, the script warns and skips verification.
    [string]$Sha256 = "1708333f79e274c7697285afe6d592ab39314e0b131e9ec6bea08ad27df62ebf",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$CaddyDir   = Join-Path $ProjectDir "deploy\caddy"
$CaddyExe   = Join-Path $CaddyDir "caddy.exe"
$Asset      = "caddy_${Version}_windows_amd64.zip"
$Url        = "https://github.com/caddyserver/caddy/releases/download/v$Version/$Asset"
$TmpZip     = Join-Path $env:TEMP $Asset
$TmpExtract = Join-Path $env:TEMP "caddy_extract_$Version"

Write-Host "Service Desk Helper - Caddy downloader" -ForegroundColor Cyan
Write-Host "  Version : $Version"
Write-Host "  Target  : $CaddyExe"

if ((Test-Path $CaddyExe) -and (-not $Force)) {
    Write-Host "caddy.exe already present. Use -Force to re-download." -ForegroundColor Yellow
    & $CaddyExe version
    exit 0
}

New-Item -ItemType Directory -Force -Path $CaddyDir | Out-Null

Write-Host "Downloading $Url ..." -ForegroundColor Cyan
# Prefer curl.exe (present in System32 on this server); fall back to Invoke-WebRequest.
$curl = Get-Command curl.exe -ErrorAction SilentlyContinue
if ($curl) {
    & curl.exe -L --fail --silent --show-error -o $TmpZip $Url
    if ($LASTEXITCODE -ne 0) { throw "curl download failed (exit $LASTEXITCODE)" }
} else {
    Invoke-WebRequest -Uri $Url -OutFile $TmpZip -UseBasicParsing
}

if (-not (Test-Path $TmpZip)) { throw "Download did not produce $TmpZip" }

if ($Sha256 -ne "") {
    Write-Host "Verifying SHA-256 ..." -ForegroundColor Cyan
    $actual = (Get-FileHash -Algorithm SHA256 -Path $TmpZip).Hash
    if ($actual -ne $Sha256.ToUpper()) {
        Remove-Item $TmpZip -Force -ErrorAction SilentlyContinue
        throw "SHA-256 MISMATCH!`n  expected: $($Sha256.ToUpper())`n  actual:   $actual`nAborting - the download may be corrupt or tampered."
    }
    Write-Host "  OK ($actual)" -ForegroundColor Green
} else {
    Write-Host "WARNING: no -Sha256 provided; skipping integrity check." -ForegroundColor Yellow
}

Write-Host "Extracting ..." -ForegroundColor Cyan
if (Test-Path $TmpExtract) { Remove-Item $TmpExtract -Recurse -Force }
Expand-Archive -Path $TmpZip -DestinationPath $TmpExtract -Force

$srcExe = Join-Path $TmpExtract "caddy.exe"
if (-not (Test-Path $srcExe)) { throw "caddy.exe not found in the archive at $srcExe" }
Copy-Item -Path $srcExe -Destination $CaddyExe -Force

Remove-Item $TmpZip -Force -ErrorAction SilentlyContinue
Remove-Item $TmpExtract -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "Installed:" -ForegroundColor Green
& $CaddyExe version
Write-Host ""
Write-Host "Validating Caddyfile syntax ..." -ForegroundColor Cyan
& $CaddyExe validate --config (Join-Path $CaddyDir "Caddyfile") --adapter caddyfile
Write-Host ""
Write-Host "Done. Next: follow deploy\CADDY_RUNBOOK.md (elevated) to free port 80 and start the proxy." -ForegroundColor Cyan
