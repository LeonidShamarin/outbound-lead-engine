<#
.SYNOPSIS
    Export a TLS-interception root CA (e.g. Avast Web Shield) so Docker builds can
    verify HTTPS on this machine without turning verification off.

.DESCRIPTION
    Antivirus HTTPS scanning re-signs every site with its own root, which Windows
    trusts and a Linux container does not, so `pip install` fails with
    CERTIFICATE_VERIFY_FAILED. The fix is to add that one root to the build, not to
    disable verification. The file goes to .certs/ (git-ignored) and reaches the
    image only as a BuildKit secret during `pip install`; it is not stored in any layer.

.EXAMPLE
    .\scripts\export_local_ca.ps1
    .\scripts\export_local_ca.ps1 -SubjectMatch 'Avast'
#>
[CmdletBinding()]
param(
    [string]$SubjectMatch = 'Avast Web/Mail Shield Root'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$OutDir = Join-Path $Root '.certs'
$Out = Join-Path $OutDir 'extra-ca.pem'

# All matches, not the first: Avast regenerates its root (seen 2026-09-23), the old
# one stays in the store, and exporting only the stale one broke every build.
$certs = @(Get-ChildItem Cert:\LocalMachine\Root, Cert:\CurrentUser\Root |
    Where-Object { $_.Subject -like "*$SubjectMatch*" -and $_.NotAfter -gt (Get-Date) } |
    Sort-Object Thumbprint -Unique)

if ($certs.Count -eq 0) {
    Write-Host "No valid root certificate matching '$SubjectMatch'; nothing to export." -ForegroundColor Yellow
    exit 0
}

New-Item -ItemType Directory -Force $OutDir | Out-Null
$pem = ($certs | ForEach-Object {
    $b64 = [Convert]::ToBase64String($_.RawData, 'InsertLineBreaks').Replace("`r", '')
    "-----BEGIN CERTIFICATE-----`n" + $b64 + "`n-----END CERTIFICATE-----`n"
}) -join ''
[IO.File]::WriteAllText($Out, $pem, (New-Object Text.UTF8Encoding $false))
Write-Host "exported $($certs.Count) root(s) matching '$SubjectMatch' -> $Out"
