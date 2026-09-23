<#
.SYNOPSIS
    Run CLI commands that call the real LLM, in the capped test image, with network.

.DESCRIPTION
    Same memory cap as the test runner (kernel-enforced, swap equal to memory). The
    Groq key is read from -KeyFile (a line like GROQ_API_KEY=gsk_...) into this
    process's environment and handed to docker as `-e GROQ_API_KEY` without a value,
    so it never appears on a command line or in a log.

    With -WithDb a throwaway capped Postgres is started on a private bridge network and
    removed afterwards; DATABASE_URL points at it.

    If an antivirus intercepts HTTPS, the root exported by export_local_ca.ps1 is
    appended to the container's CA bundle and passed as SSL_CERT_FILE. Verification
    stays on.

.EXAMPLE
    .\scripts\run_live_capped.ps1 -Command 'python -m leadengine eval-replies'
    .\scripts\run_live_capped.ps1 -WithDb -Command 'python -m leadengine migrate && python -m leadengine seed'
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$Command,
    [string]$KeyFile = 'D:\secrets\groq-api-key.txt',
    [int]$MemoryMB = 1024,
    [int]$TimeoutSec = 1800,
    [string]$Log = '',
    [switch]$WithDb
)

$ErrorActionPreference = 'Stop'
$Root  = Split-Path -Parent $PSScriptRoot
$Name  = (Split-Path -Leaf $Root).ToLower()
$Image = "$Name-tests"
$Net   = "$Name-live-$PID"
$DbCt  = "$Name-livedb-$PID"
if (-not $Log) { $Log = Join-Path $Root '.test-logs\live.log' }

$m = [regex]::Match((Get-Content $KeyFile -Raw), 'gsk_[A-Za-z0-9]+')
if (-not $m.Success) { Write-Host "no gsk_ key in $KeyFile" -ForegroundColor Red; exit 2 }
$env:GROQ_API_KEY = $m.Value

$prefix = 'set -e; if [ -s /work/.certs/extra-ca.pem ]; then cat /etc/ssl/certs/ca-certificates.crt /work/.certs/extra-ca.pem > /tmp/ca.pem; export SSL_CERT_FILE=/tmp/ca.pem; fi; '
$code = 1
try {
    $dockerArgs = @('run', '--rm', '--memory', "${MemoryMB}m", '--memory-swap', "${MemoryMB}m", '--cpus', '2',
                    '-v', "$($Root):/work", '-w', '/work', '-e', 'PYTHONPATH=src', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                    '-e', 'GROQ_API_KEY')
    if ($WithDb) {
        docker network create $Net | Out-Null
        docker run -d --rm --name $DbCt --network $Net --memory 384m --memory-swap 384m `
            -e POSTGRES_PASSWORD=live -e POSTGRES_USER=live -e POSTGRES_DB=live postgres:16-alpine | Out-Null
        $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        for ($i = 0; $i -lt 60; $i++) {
            docker exec $DbCt psql -U live -d live -c 'SELECT 1' 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { break }
            Start-Sleep -Milliseconds 500
        }
        $ErrorActionPreference = $prev
        $dockerArgs += @('--network', $Net, '-e', "DATABASE_URL=postgresql://live:live@$($DbCt):5432/live")
    }
    $dockerArgs += @('--entrypoint', 'sh', $Image, '-c', "`"$prefix$Command`"")
    $proc = Start-Process -FilePath 'docker' -ArgumentList $dockerArgs -PassThru -NoNewWindow `
                          -RedirectStandardOutput $Log -RedirectStandardError "$Log.err"
    $null = $proc.Handle
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while (-not $proc.HasExited) {
        Start-Sleep -Milliseconds 500
        if ($sw.Elapsed.TotalSeconds -gt $TimeoutSec) { $proc.Kill(); Write-Host "TIMEOUT" -ForegroundColor Red; break }
    }
    $proc.WaitForExit()
    $code = $proc.ExitCode
}
finally {
    Remove-Item Env:\GROQ_API_KEY -ErrorAction SilentlyContinue
    $ErrorActionPreference = 'Continue'
    if ($WithDb) { docker rm -f $DbCt 2>&1 | Out-Null; docker network rm $Net 2>&1 | Out-Null }
}
if ($code -eq 137) { Write-Host "MEMLIMIT: killed at $MemoryMB MB" -ForegroundColor Red }
Write-Host ("exit {0}; output in {1}" -f $code, $Log)
exit $code
