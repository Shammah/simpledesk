<#
Copies the server's nightly backups to this computer, so a copy exists even if
the server is lost. Run it once a week (or set it up in Windows Task Scheduler).

    .\deploy\download-backups.ps1 -Server root@YOUR.SERVER.IP

Backups land in a "server-backups" folder next to this project. They contain
client details and mail/WhatsApp passwords: keep them private.
#>
param([Parameter(Mandatory = $true)][string]$Server)
$ErrorActionPreference = "Stop"
$target = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "server-backups"
New-Item -ItemType Directory -Force $target | Out-Null
scp "${Server}:/var/backups/simpledesk/*.zip" $target
if ($LASTEXITCODE -ne 0) { throw "Download failed." }
Write-Host "Backups copied to $target"
