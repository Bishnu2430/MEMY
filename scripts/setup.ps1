<#
.SYNOPSIS
  Memy setup for Windows: checks/installs tools, creates .env with generated secrets, builds and starts the stack,
  and waits until it is healthy. Safe to re-run: existing .env values are kept.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -GroqKey gsk_xxx -Yes
#>
[CmdletBinding()]
param(
  [string]$GroqKey = $env:GROQ_API_KEY,
  [string]$ElevenLabsKey = $env:ELEVENLABS_API_KEY,
  [switch]$Install,   # install missing tools with winget without asking
  [switch]$NoStart,   # only prepare .env
  [switch]$Rebuild,   # rebuild images without cache
  [switch]$Yes        # answer yes to every prompt
)
# Native tools (docker, winget) write progress and warnings to stderr; in Windows PowerShell 5.1 that becomes an
# error record, so failures are detected from exit codes instead of $ErrorActionPreference = "Stop".
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$WebUrl = "http://localhost:8080"
$EnvFile = Join-Path $Root ".env"

function Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor White }
function Ok($m)   { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  [x]  $m" -ForegroundColor Red; exit 1 }
function Have($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }
function Confirm-Action($q) {
  if ($Yes -or $Install) { return $true }
  $a = Read-Host "  $q [y/N]"
  return $a -match '^[Yy]'
}
function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Winget-Install($id, $name) {
  if (-not (Have winget)) { Fail "$name is missing and winget is unavailable. Install it manually and re-run." }
  Write-Host "  Installing $name with winget..."
  winget install --id $id -e --accept-source-agreements --accept-package-agreements | Out-Host
  Refresh-Path
}

# ---------- 1. tools ----------
Step "1/5  Checking required tools"
if (Have docker) { Ok ("Docker " + ((docker --version) -replace 'Docker version ', '' -replace ',.*', '')) }
else {
  Warn "Docker Desktop is not installed (required). It needs WSL 2 (installed automatically on Windows 10/11)."
  if (Confirm-Action "Install Docker Desktop now with winget?") {
    Winget-Install "Docker.DockerDesktop" "Docker Desktop"
    Warn "Docker Desktop installed. If this is the first install, sign out/in or reboot, start Docker Desktop once, then re-run this script."
    if (-not (Have docker)) { exit 1 }
  } else { Fail "Docker Desktop is required: https://www.docker.com/products/docker-desktop/" }
}
docker compose version 2>&1 | Out-Null
$composeOk = ($LASTEXITCODE -eq 0)
if ($composeOk) { Ok ("Docker Compose " + (docker compose version --short)) } else { Fail "Docker Compose v2 is required (bundled with Docker Desktop)." }

if (Have git) { Ok ("git " + ((git --version) -replace 'git version ', '')) }
else {
  Warn "git not found (only needed to pull updates)."
  if (Confirm-Action "Install Git now with winget?") { Winget-Install "Git.Git" "Git" }
}
if (Have uv) { Ok ("uv " + ((uv --version) -split ' ')[1] + " (terminal client and evals)") }
else {
  Warn "uv not found (optional: used by the terminal client and the eval scripts)."
  if (Confirm-Action "Install uv now with winget?") { Winget-Install "astral-sh.uv" "uv" }
}

# ---------- 2. docker daemon ----------
Step "2/5  Making sure Docker is running"
function Docker-Up { docker info 2>&1 | Out-Null; return ($LASTEXITCODE -eq 0) }
if (-not (Docker-Up)) {
  $dd = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
  if (Test-Path $dd) { Write-Host "  Starting Docker Desktop..."; Start-Process $dd }
  Write-Host -NoNewline "  waiting for the Docker engine"
  for ($i = 0; $i -lt 60 -and -not (Docker-Up); $i++) { Write-Host -NoNewline "."; Start-Sleep -Seconds 3 }
  Write-Host ""
}
if (-not (Docker-Up)) { Fail "Docker is not running. Start Docker Desktop and re-run." }
$memRaw = (docker info --format '{{.MemTotal}}' 2>$null | Select-Object -Last 1)
$memGb = if ($memRaw -match '^\d+$') { [math]::Floor([double]$memRaw / 1GB) } else { 0 }
Ok "Docker engine is running ($memGb GB memory available to containers)"
if ($memGb -lt 4) { Warn "Less than 4 GB of memory for Docker; increase it in Docker Desktop > Settings > Resources." }
foreach ($port in 8080, 8000) {
  $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  $ours = ((docker compose ps --format '{{.Ports}}' 2>$null) -join " ") -match ":$port->"
  if ($busy -and -not $ours) { Warn "Port $port is already in use by another program; the web UI (8080) or API (8000) may fail to start." }
}

# ---------- 3. .env ----------
Step "3/5  Preparing configuration (.env)"
function New-Secret([int]$len = 24) {
  $chars = [char[]]"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
  $bytes = New-Object byte[] $len
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}
function New-Key32 {
  $bytes = New-Object byte[] 32
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  [Convert]::ToBase64String($bytes)
}
if (-not (Test-Path $EnvFile)) { Copy-Item (Join-Path $Root ".env.example") $EnvFile -ErrorAction Stop; Ok "created .env from .env.example" }
$lines = [System.Collections.Generic.List[string]]::new([string[]](Get-Content $EnvFile -Encoding UTF8))
function Get-EnvValue($key) {
  foreach ($l in $lines) { if ($l -match "^$([regex]::Escape($key))=(.*)$") { return $Matches[1] } }
  return ""
}
function Set-EnvValue($key, $value) {
  for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match "^$([regex]::Escape($key))=") { $lines[$i] = "$key=$value"; return } }
  $lines.Add("$key=$value")
}
function Fill($key, $value) { if (-not (Get-EnvValue $key)) { Set-EnvValue $key $value; Write-Host "  generated $key" -ForegroundColor DarkGray } }

Fill "POSTGRES_PASSWORD" (New-Secret 24)
Fill "APP_DB_PASSWORD" (New-Secret 24)
Fill "JWT_SECRET" (New-Secret 48)
Fill "MASTER_KEY" (New-Key32)
Fill "SEED_ADMIN_EMAIL" "admin@memy.local"; Fill "SEED_ADMIN_PASSWORD" (New-Secret 16)
Fill "SEED_ALICE_EMAIL" "alice@memy.local"; Fill "SEED_ALICE_PASSWORD" (New-Secret 16)
Fill "SEED_BOB_EMAIL" "bob@memy.local";     Fill "SEED_BOB_PASSWORD" (New-Secret 16)

function Read-Secret($prompt) {
  $s = Read-Host -AsSecureString $prompt
  [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
}
if ($GroqKey) { Set-EnvValue "GROQ_API_KEY" $GroqKey }
if (-not (Get-EnvValue "GROQ_API_KEY") -and -not $Yes) {
  Write-Host "  A Groq API key is required for agents (free at https://console.groq.com/keys)."
  $k = Read-Secret "  Paste GROQ_API_KEY (input hidden, Enter to skip)"
  if ($k) { Set-EnvValue "GROQ_API_KEY" $k }
}
if ($ElevenLabsKey) { Set-EnvValue "ELEVENLABS_API_KEY" $ElevenLabsKey }
if (-not (Get-EnvValue "ELEVENLABS_API_KEY") -and -not $Yes) {
  $k = Read-Secret "  Paste ELEVENLABS_API_KEY (optional, input hidden, Enter to skip)"
  if ($k) { Set-EnvValue "ELEVENLABS_API_KEY" $k }
}
# UTF-8 without BOM and LF line endings: docker compose misreads the first variable after a BOM.
[System.IO.File]::WriteAllText($EnvFile, (($lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))
if (Get-EnvValue "GROQ_API_KEY") { Ok "Groq key configured" } else { Warn "No GROQ_API_KEY yet: the app starts, but agents cannot run until you add it to .env." }
if (Get-EnvValue "ELEVENLABS_API_KEY") { Ok "ElevenLabs key configured" } else { Warn "No ELEVENLABS_API_KEY: media features stay disabled (optional)." }

if ($NoStart) { Step "Done (.env prepared, containers not started)"; exit 0 }

# ---------- 4. build & start ----------
Step "4/5  Building and starting the stack (first run downloads ~3 GB and takes a few minutes)"
if ($Rebuild) { docker compose build --no-cache 2>&1 | ForEach-Object { "$_" } | Out-Host; if ($LASTEXITCODE -ne 0) { Fail "Build failed." } }
docker compose up -d --build 2>&1 | ForEach-Object { "$_" } | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "docker compose up failed. See the output above." }

# ---------- 5. health ----------
Step "5/5  Waiting for the platform to become healthy"
$healthy = $false
for ($i = 0; $i -lt 120; $i++) {
  try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "$WebUrl/api/health"; if ($r.StatusCode -eq 200) { $healthy = $true; break } } catch { }
  Start-Sleep -Seconds 5
}
if (-not $healthy) { docker compose ps; Fail "Timed out. Inspect logs with: docker compose logs api" }
Ok "All services are up"
docker compose ps --format "  {{.Name}}`t{{.Status}}"

Write-Host ""
Write-Host "Memy is running: $WebUrl" -ForegroundColor White
Write-Host ("  Sign in as     " + (Get-EnvValue "SEED_ADMIN_EMAIL") + "   (password: SEED_ADMIN_PASSWORD in .env)")
Write-Host "  Stop           docker compose down"
Write-Host "  Logs           docker compose logs -f api orchestrator worker"
Write-Host "  Verify         uv run evals/run_all.py"
Write-Host "  Terminal       uv run cli/hub.py login"
