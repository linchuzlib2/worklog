$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

Write-Host 'Worklog local document editor setup' -ForegroundColor Cyan
$baseUrl = (Read-Host 'Render site URL, for example https://worklog.onrender.com').Trim().TrimEnd('/')
if ($baseUrl -notmatch '^https://[^/]+$') {
  throw 'The site URL must be an https:// root address.'
}

$secureToken = Read-Host 'Enter the Render LOCAL_EDITOR_TOKEN value' -AsSecureString
if ($secureToken.Length -lt 32) {
  throw 'The sync token must contain at least 32 characters.'
}

$installPath = Join-Path $env:APPDATA 'WorklogLocalEditor'
New-Item -ItemType Directory -Path $installPath -Force | Out-Null
$helperPath = Join-Path $installPath 'Open-LocalDocument.ps1'
$configPath = Join-Path $installPath 'config.json'
$helperUrl = "$baseUrl/static/local-editor/Open-LocalDocument.ps1"
Invoke-WebRequest -Uri $helperUrl -OutFile $helperPath -UseBasicParsing

$config = @{
  BaseUrl = $baseUrl
  Token = ($secureToken | ConvertFrom-SecureString)
} | ConvertTo-Json
Set-Content -Path $configPath -Value $config -Encoding UTF8

$protocolPath = 'HKCU:\Software\Classes\worklog-editor'
$commandPath = Join-Path $protocolPath 'shell\open\command'
New-Item -Path $commandPath -Force | Out-Null
Set-Item -Path $protocolPath -Value 'URL: Worklog Local Document Editor'
New-ItemProperty -Path $protocolPath -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
$command = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}" "%1"' -f $helperPath
Set-Item -Path $commandPath -Value $command

Write-Host 'Installed. Return to File Manager and select Local Edit.' -ForegroundColor Green
Read-Host 'Press Enter to exit'