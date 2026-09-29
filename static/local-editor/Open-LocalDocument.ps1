param([Parameter(Position = 0)][string]$ProtocolUri)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Stop-WithMessage([string]$Message) {
  Write-Host $Message -ForegroundColor Red
  Read-Host 'Press Enter to close'
  exit 1
}

if ($ProtocolUri -notmatch '^worklog-editor://open/(\d+)$') {
  Stop-WithMessage 'Invalid editor link. Reopen the document from Worklog File Manager.'
}
$attachmentId = $Matches[1]

$configPath = Join-Path $env:APPDATA 'WorklogLocalEditor\config.json'
if (-not (Test-Path $configPath)) {
  Stop-WithMessage 'The helper is not installed. Run the installer first.'
}
$config = Get-Content -Path $configPath -Raw | ConvertFrom-Json
try {
  $secureToken = ConvertTo-SecureString $config.Token
  $token = [Net.NetworkCredential]::new('', $secureToken).Password
} catch {
  Stop-WithMessage 'Cannot read the local token. Run the installer again.'
}

$baseUrl = ([string]$config.BaseUrl).TrimEnd('/')
$headers = @{ Authorization = "Bearer $token" }
$apiBase = "$baseUrl/api/local-editor/$attachmentId"
$sessionPath = Join-Path $env:LOCALAPPDATA "WorklogLocalEditor\$attachmentId"
New-Item -ItemType Directory -Path $sessionPath -Force | Out-Null

try {
  $metadata = Invoke-RestMethod -Uri $apiBase -Headers $headers -Method Get
  $fileName = [IO.Path]::GetFileName([string]$metadata.filename)
  if ([IO.Path]::GetExtension($fileName).ToLowerInvariant() -notin @('.doc', '.docx', '.xls', '.xlsx', '.pdf')) {
    Stop-WithMessage 'This helper supports Word, Excel, and PDF files only.'
  }
  $filePath = Join-Path $sessionPath $fileName
  $download = Invoke-WebRequest -Uri "$apiBase/content" -Headers $headers -Method Get -OutFile $filePath -UseBasicParsing -PassThru
  $revision = ([string]$download.Headers.ETag).Trim('"')
  if (-not $revision) {
    Stop-WithMessage 'The server returned no revision. Editing cannot continue safely.'
  }
} catch {
  Stop-WithMessage "Download failed: $($_.Exception.Message)"
}

$lastUploadedHash = (Get-FileHash -Path $filePath -Algorithm SHA256).Hash.ToLowerInvariant()
try {
  Start-Process -FilePath $filePath
} catch {
  Stop-WithMessage "Could not open the file with its Windows default app: $($_.Exception.Message)"
}

$watcher = New-Object IO.FileSystemWatcher
$watcher.Path = $sessionPath
$watcher.Filter = $fileName
$watcher.NotifyFilter = [IO.NotifyFilters]::LastWrite -bor [IO.NotifyFilters]::Size -bor [IO.NotifyFilters]::FileName
$watcher.EnableRaisingEvents = $true
$sourceIds = @("WorklogLocalEditor-$attachmentId-Changed", "WorklogLocalEditor-$attachmentId-Created", "WorklogLocalEditor-$attachmentId-Renamed")
Register-ObjectEvent -InputObject $watcher -EventName Changed -SourceIdentifier $sourceIds[0] | Out-Null
Register-ObjectEvent -InputObject $watcher -EventName Created -SourceIdentifier $sourceIds[1] | Out-Null
Register-ObjectEvent -InputObject $watcher -EventName Renamed -SourceIdentifier $sourceIds[2] | Out-Null

Write-Host "Watching for saves: $fileName" -ForegroundColor Cyan
Write-Host 'Changes upload automatically. Closing this window stops monitoring.'
$deadline = [DateTime]::UtcNow.AddHours(24)
try {
  while ([DateTime]::UtcNow -lt $deadline) {
    $change = Wait-Event -Timeout 10
    if (-not $change) { continue }
    Remove-Event -EventIdentifier $change.EventIdentifier -ErrorAction SilentlyContinue

    while ($quietEvent = Wait-Event -Timeout 2) {
      Remove-Event -EventIdentifier $quietEvent.EventIdentifier -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path $filePath)) { continue }

    try {
      $currentHash = (Get-FileHash -Path $filePath -Algorithm SHA256).Hash.ToLowerInvariant()
    } catch {
      continue
    }
    if ($currentHash -eq $lastUploadedHash) { continue }

    try {
      Invoke-WebRequest -Uri "$apiBase/content" -Headers ($headers + @{ 'If-Match' = $revision }) -Method Put -InFile $filePath -ContentType 'application/octet-stream' -UseBasicParsing | Out-Null
      $revision = $currentHash
      $lastUploadedHash = $currentHash
      Write-Host "Uploaded: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Green
    } catch {
      $statusCode = 0
      if ($_.Exception.Response) { $statusCode = [int]$_.Exception.Response.StatusCode }
      if ($statusCode -eq 409) {
        Write-Host 'The cloud version changed. Close this document and reopen the latest version.' -ForegroundColor Yellow
        break
      }
      Write-Host "Upload failed: $($_.Exception.Message)" -ForegroundColor Red
    }
  }
} finally {
  $sourceIds | ForEach-Object { Unregister-Event -SourceIdentifier $_ -ErrorAction SilentlyContinue }
  $watcher.Dispose()
}

Write-Host 'Document monitoring has ended.'
Read-Host 'Press Enter to close'