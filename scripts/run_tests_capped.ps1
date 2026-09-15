<#
.SYNOPSIS
    Run pytest under a hard, kernel-enforced memory cap, next to a capped Postgres.

.DESCRIPTION
    Same runner as in kb-rag-assistant / doc-to-crm / inbox-agent, with one change:
    these tests need a real Postgres, so instead of `--network none` the test
    container joins a private Docker network that holds only a Postgres container.
    Both containers are capped (`--memory` equal to `--memory-swap`, so there is no
    escape into swap), and both are removed in `finally`, even on timeout.

    Why Docker and not a Windows Job Object: the Job Object version needed
    Add-Type with P/Invoke and Avast quarantines it as IDP.HELU.PSD11.

    Output goes to .test-logs\pytest.log; the terminal gets one summary line.

.EXAMPLE
    .\scripts\run_tests_capped.ps1
    .\scripts\run_tests_capped.ps1 -Path tests\test_schema_fixes.py
#>
[CmdletBinding()]
param(
    [int]$MemoryMB   = 1024,
    [int]$DbMemoryMB = 384,
    [int]$TimeoutSec = 300,
    [string]$Path    = "",
    [switch]$Rebuild
)

$ErrorActionPreference = 'Stop'

$Root    = Split-Path -Parent $PSScriptRoot
$Name    = (Split-Path -Leaf $Root).ToLower()
$Image   = "$Name-tests"
$Net     = "$Name-testnet-$PID"
$DbName  = "$Name-testdb-$PID"
$LogDir  = Join-Path $Root '.test-logs'
$Log     = Join-Path $LogDir 'pytest.log'
$PgImage = 'postgres:16-alpine'
$Dockerfile = if (Test-Path (Join-Path $Root 'Dockerfile.tests')) { 'Dockerfile.tests' } else { 'Dockerfile' }

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

try {
    docker version --format '{{.Server.Version}}' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "docker" }
} catch {
    Write-Host "Docker is not available: start Docker Desktop. No cap, no tests." -ForegroundColor Red
    exit 3
}

# --- image ------------------------------------------------------------------
$needBuild = $Rebuild.IsPresent
if (-not $needBuild) {
    $existing = docker images -q $Image 2>$null
    if (-not $existing) {
        $needBuild = $true
    } else {
        $imageDate = [datetime]::Parse((docker inspect -f '{{.Created}}' $Image))
        $newest = @('requirements.txt', 'requirements-dev.txt', $Dockerfile) |
            ForEach-Object { Join-Path $Root $_ } |
            Where-Object { Test-Path $_ } |
            ForEach-Object { (Get-Item $_).LastWriteTime } |
            Sort-Object -Descending | Select-Object -First 1
        if ($newest -gt $imageDate) { $needBuild = $true }
    }
}
if ($needBuild) {
    Write-Host "building $Image ($Dockerfile) ..." -ForegroundColor DarkGray
    $buildArgs = @('build', '-q', '-f', (Join-Path $Root $Dockerfile), '-t', $Image)
    $extraCa = Join-Path $Root '.certs\extra-ca.pem'
    if (Test-Path $extraCa) { $buildArgs += @('--secret', "id=extra_ca,src=$extraCa") }
    docker @buildArgs $Root | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "image build failed" -ForegroundColor Red; exit 4 }
}

$code = 1
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
    # --- capped Postgres on a private network -------------------------------
    docker network create --internal $Net | Out-Null
    docker run -d --rm --name $DbName --network $Net `
        --memory "${DbMemoryMB}m" --memory-swap "${DbMemoryMB}m" --cpus 1 `
        -e POSTGRES_PASSWORD=test -e POSTGRES_USER=test -e POSTGRES_DB=test `
        $PgImage -c fsync=off -c synchronous_commit=off -c full_page_writes=off | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "postgres container failed to start" -ForegroundColor Red; exit 5 }

    # Bounded wait: 60 tries x 500 ms. pg_isready alone passes during the init
    # restart, so readiness is a real query.
    # PS 5.1 turns a native command's stderr into an ErrorRecord, which 'Stop'
    # would make fatal on the first "not ready yet" attempt.
    $ready = $false
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    for ($i = 0; $i -lt 60; $i++) {
        docker exec $DbName psql -U test -d test -c 'SELECT 1' 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Milliseconds 500
    }
    $ErrorActionPreference = $prevEap
    if (-not $ready) { Write-Host "postgres did not become ready in 30 s" -ForegroundColor Red; exit 5 }

    # --- test run -----------------------------------------------------------
    $target = if ($Path) { $Path.Replace('\', '/') } else { 'tests' }
    $dockerArgs = @(
        'run', '--rm',
        '--memory', "${MemoryMB}m",
        '--memory-swap', "${MemoryMB}m",
        '--cpus', '2',
        '--network', $Net,
        '-v', "$($Root):/work",
        '-w', '/work',
        '-e', 'PYTHONDONTWRITEBYTECODE=1',
        '-e', "DATABASE_URL=postgresql://test:test@$($DbName):5432/test",
        '--entrypoint', 'python',
        $Image,
        '-m', 'pytest', $target, '-q', '--no-header', '--tb=short', '-p', 'no:cacheprovider'
    )
    $proc = Start-Process -FilePath 'docker' -ArgumentList $dockerArgs -PassThru -NoNewWindow `
                          -RedirectStandardOutput $Log -RedirectStandardError "$Log.err"
    $null = $proc.Handle  # caches the handle; without it PS 5.1 loses ExitCode

    while (-not $proc.HasExited) {
        Start-Sleep -Milliseconds 300
        if ($sw.Elapsed.TotalSeconds -gt $TimeoutSec) {
            Write-Host "TIMEOUT (> $TimeoutSec s), stopped" -ForegroundColor Red
            $proc.Kill()
            exit 1
        }
    }
    $proc.WaitForExit()
    $code = $proc.ExitCode
}
finally {
    $ErrorActionPreference = 'Continue'
    docker rm -f $DbName 2>&1 | Out-Null
    docker network rm $Net 2>&1 | Out-Null
}
$sw.Stop()

$summary = ''
if (Test-Path $Log) {
    $hits = @(Get-Content $Log -Tail 40 -ErrorAction SilentlyContinue |
              Where-Object { $_ -match '(passed|failed|error|no tests ran)' })
    if ($hits.Count -gt 0) { $summary = $hits[-1].Trim() }
}

if ($code -eq 137) {
    Write-Host ("MEMLIMIT: tests exceeded {0} MB and were killed by the kernel" -f $MemoryMB) -ForegroundColor Red
    Write-Host "details in $Log" -ForegroundColor Yellow
    exit 1
}

$color = if ($code -eq 0) { 'Green' } else { 'Red' }
Write-Host ("{0,-8} cap {1} MB (db {2} MB) | {3,5:N1} s | {4}" -f `
    $(if ($code -eq 0) { 'PASS' } else { "FAIL ($code)" }), $MemoryMB, $DbMemoryMB, `
    $sw.Elapsed.TotalSeconds, $summary) -ForegroundColor $color
if ($code -ne 0) { Write-Host "details in $Log" -ForegroundColor Yellow }
exit $code
