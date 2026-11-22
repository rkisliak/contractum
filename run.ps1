# Starts the local trial / dev Contractum stack (Windows PowerShell 5.1+ / PowerShell 7); production uses Helm.
#
#   .\run.ps1            check prerequisites, then start everything and wait until healthy
#   .\run.ps1 check      only check prerequisites
#   .\run.ps1 smoke      log in as admin through the gateway (needs bash: Git Bash or WSL)
#   .\run.ps1 e2e [args] run the end-to-end tests in a container (default: tests/e2e)
#   .\run.ps1 status     show containers
#   .\run.ps1 logs [svc] follow logs (all services or one)
#   .\run.ps1 down       stop and remove containers (data is kept)
#   .\run.ps1 reset      stop and delete ALL data and generated secrets (asks first)
#
# No environment variables are required: the dev-init container generates every secret and the
# TLS certificate on first start. See docs/dev/local-stack.md (production: docs/admin/deploy-kubernetes.md).
# If scripts are blocked: powershell -ExecutionPolicy Bypass -File .\run.ps1
param(
    [Parameter(Position = 0)][string]$Command = "up",
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$Port = 8443
$Base = "https://localhost:$Port"
$MinCompose = [version]"2.21"
$MinMemGiB = 6
$WaitTimeout = if ($env:WAIT_TIMEOUT) { $env:WAIT_TIMEOUT } else { "300" }
$script:Failed = $false
# Real curl binary (curl.exe on Windows; in Windows PowerShell 5.1 "curl" is an alias, so look up applications only).
$Curl = Get-Command -CommandType Application curl.exe, curl -ErrorAction SilentlyContinue | Select-Object -First 1

function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  [X]  $m" -ForegroundColor Red; $script:Failed = $true }

function Invoke-Native {
    # Run a native command and stop on a non-zero exit code.
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') failed (exit $LASTEXITCODE)" }
}

function Test-StackRunning {
    $id = (& docker compose ps -q --status running traefik 2>$null)
    return [bool]$id
}

function Test-PortInUse {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        return ($task.Wait(500) -and $client.Connected)
    } catch { return $false } finally { $client.Dispose() }
}

function Test-Prereqs {
    Write-Host "Checking prerequisites..."
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Fail "docker not found - install Docker Desktop first"; return $false
    }
    Ok "docker installed"

    & docker info *> $null
    if ($LASTEXITCODE -ne 0) { Fail "Docker daemon not reachable - start Docker Desktop"; return $false }
    Ok "Docker daemon running"

    $cv = (& docker compose version --short 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $cv) {
        Fail "'docker compose' (v2) not available - update Docker Desktop"
    } else {
        $clean = ($cv.Trim() -replace '^v', '') -replace '[^0-9.].*$', ''
        if ([version]$clean -ge $MinCompose) { Ok "docker compose $clean" }
        else { Fail "docker compose $clean is too old (need >= $MinCompose)" }
    }

    $mem = (& docker info --format '{{.MemTotal}}' 2>$null)
    if ($mem) {
        $gib = [math]::Floor([double]$mem / 1GB)
        if ($gib -lt $MinMemGiB) { Warn "Docker has $gib GiB memory; $MinMemGiB+ GiB recommended (Docker Desktop > Settings > Resources)" }
        else { Ok "Docker memory $gib GiB" }
    }

    if (Test-StackRunning) { Ok "port $Port held by this stack (already running)" }
    elseif (Test-PortInUse) { Fail "port $Port is in use by another program - free it before starting" }
    else { Ok "port $Port free" }

    if ($Curl) { Ok "curl installed" }
    else { Warn "curl not found - only the post-start gateway check is skipped" }
    if (Get-Command bash -ErrorAction SilentlyContinue) { Ok "bash found (smoke test available)" }
    else { Warn "bash not found - '.\run.ps1 smoke' needs Git Bash or WSL" }

    if ($script:Failed) { Write-Host "Fix the items marked [X] and run again." -ForegroundColor Red; return $false }
    Write-Host "All required prerequisites OK." -ForegroundColor Green
    return $true
}

function Start-Stack {
    if (-not (Test-Prereqs)) { exit 1 }
    Write-Host ""
    Write-Host "Building and starting the stack (first run builds images and can take several minutes)..."
    Invoke-Native docker @("compose", "up", "-d", "--build", "--wait", "--wait-timeout", $WaitTimeout)
    Write-Host ""
    if ($Curl -and (Test-Path .dev/ca.crt)) {
        $null_out = if ($IsWindows -or $env:OS -eq "Windows_NT") { "NUL" } else { "/dev/null" }
        $code = (& $Curl.Source -sS -o $null_out -w "%{http_code}" --cacert .dev/ca.crt "$Base/api/v1/openapi.json" 2>$null)
        if ($code -eq "200") { Ok "gateway answers at $Base" }
        else { Warn "stack started but the gateway did not answer 200 yet - check '.\run.ps1 status' / '.\run.ps1 logs api-docs'" }
    }
    # The Web UI is https://localhost:8443/ (the ui container behind the gateway).
    Write-Host @"

Stack is up.
  Web UI:                 $Base/
  API docs (Swagger UI):  $Base/api/v1/docs/
  Merged OpenAPI:         $Base/api/v1/openapi.json
  TLS CA to trust:        .dev\ca.crt   (e.g. curl.exe --cacert .dev\ca.crt ...)
  Admin login:            user 'admin'; first-time secret in .dev\bootstrap_admin_secret
                          (must be rotated on first login - '.\run.ps1 smoke' does it and
                           saves the new password to .dev\admin_password)
"@
}

function Invoke-E2E {
    if (-not (Test-Prereqs)) { exit 1 }
    $paths = if ($Rest -and $Rest.Count -gt 0) { $Rest } else { @("tests/e2e") }
    Invoke-Native docker @("compose", "up", "-d", "--build", "--wait", "--wait-timeout", $WaitTimeout)
    # The runner is non-root (scripts/e2e-runner-env.sh): the invoking user where `id` exists, so it
    # can write the .dev bind, else the image's user 10001; plus the Docker socket's group.
    if ((Get-Command id -ErrorAction SilentlyContinue) -and -not $env:DC_E2E_UID) {
        $hostUid = (& id -u).Trim()
        if ($hostUid -ne "0") { $env:DC_E2E_UID = $hostUid; $env:DC_E2E_GID = (& id -g).Trim() }
    }
    if (-not $env:DC_DOCKER_GID) {
        $env:DC_DOCKER_GID = (& docker run --rm --pull never --network none --entrypoint stat `
            -v /var/run/docker.sock:/var/run/docker.sock:ro contractum/identity:dev -c %g /var/run/docker.sock).Trim()
    }
    & docker compose -f compose.yaml -f compose.test.yaml --profile test `
        run --rm --build e2e pytest -m e2e -q -o asyncio_default_test_loop_scope=session @paths
    exit $LASTEXITCODE
}

function Show-Help {
    Get-Content $PSCommandPath | Select-Object -Skip 1 -First 13 | ForEach-Object { $_ -replace '^# ?', '' }
}

switch ($Command.ToLower()) {
    { $_ -in "up", "start" } { Start-Stack }
    "check" { if (-not (Test-Prereqs)) { exit 1 } }
    "smoke" {
        if (-not (Get-Command bash -ErrorAction SilentlyContinue)) {
            Write-Host "The smoke test needs bash (Git Bash or WSL) plus curl and python3." -ForegroundColor Red; exit 1
        }
        & bash scripts/skeleton-smoke.sh; exit $LASTEXITCODE
    }
    { $_ -in "e2e", "test" } { Invoke-E2E }
    { $_ -in "status", "ps" } { & docker compose ps }
    "logs" { & docker compose logs -f @Rest }
    { $_ -in "down", "stop" } { & docker compose down --remove-orphans }
    "reset" {
        $a = Read-Host "Delete ALL data, generated secrets and .dev\? [y/N]"
        if ($a -eq "y" -or $a -eq "Y") {
            & docker compose -f compose.yaml -f compose.test.yaml --profile test down -v --remove-orphans
            if (Test-Path .dev) { Remove-Item -Recurse -Force .dev }
            Write-Host "Reset done. Next '.\run.ps1' starts from scratch." -ForegroundColor Green
        } else { Write-Host "Cancelled." }
    }
    { $_ -in "help", "-h", "--help" } { Show-Help }
    default { Write-Host "unknown command: $Command" -ForegroundColor Red; Show-Help; exit 2 }
}
