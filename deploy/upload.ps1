<#
Sends SimpleDesk from this computer to your DigitalOcean server and puts it live.

First time (sets the server up):
    .\deploy\upload.ps1 -Server root@YOUR.SERVER.IP -Setup
First time, once you own a domain:
    .\deploy\upload.ps1 -Server root@YOUR.SERVER.IP -Setup -Domain help.yourdomain.com
Every update after that:
    .\deploy\upload.ps1 -Server root@YOUR.SERVER.IP

Your local data folder (database, passwords) is never sent.
#>
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [switch]$Setup,
    [string]$Domain = ""
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$package = Join-Path $env:TEMP "simpledesk-upload.tgz"

Write-Host "==> Packing the app (without .venv, data and caches)"
tar -czf $package --exclude=.venv --exclude=data --exclude=__pycache__ --exclude=.claude -C $root .
if ($LASTEXITCODE -ne 0) { throw "Packing failed." }

Write-Host "==> Sending to $Server"
scp $package "${Server}:/root/simpledesk-upload.tgz"
if ($LASTEXITCODE -ne 0) { throw "Upload failed. Check the server address and that you can sign in with ssh $Server" }

$script = if ($Setup) { "deploy/setup.sh $Domain" } else { "deploy/update.sh" }
Write-Host "==> Installing on the server"
ssh $Server "rm -rf /root/simpledesk-upload && mkdir -p /root/simpledesk-upload && tar -xzf /root/simpledesk-upload.tgz -C /root/simpledesk-upload && cd /root/simpledesk-upload && bash $script"
if ($LASTEXITCODE -ne 0) { throw "The server reported a problem; see the messages above." }
Remove-Item $package -Confirm:$false
Write-Host "==> Done."
