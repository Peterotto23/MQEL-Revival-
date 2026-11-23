# make-full-zip.ps1
# Run from the folder that CONTAINS "mqel", e.g. C:\Users\petek\Downloads\mqel-handover
#   cd C:\Users\petek\Downloads\mqel-handover
#   powershell -ExecutionPolicy Bypass -File .\make-full-zip.ps1
#
# Makes up to two zips in your Downloads folder (not next to the script):
#   mqel-readable.zip : every text file (json, csv, js, html, css, py, md, txt ...) under the size cap
#   mqel-full.zip     : every file under the size cap, binaries included
# Options:
#   -Mode Readable | Full | Both   (default Both)
#   -MaxMB 250                     (files at or above this size are skipped)
#   -IncludeSecrets                (off by default, see $blocked below)
#   -OutDir <path>                 (override the output folder; default is your Downloads folder)

param(
  [ValidateSet('Both','Readable','Full')][string]$Mode = 'Both',
  [int]$MaxMB = 250,
  [switch]$IncludeSecrets,
  [string]$OutDir
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$root     = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$rootFull = (Resolve-Path -LiteralPath $root).Path.TrimEnd('\')
$maxBytes = [int64]$MaxMB * 1MB

# Output folder: Downloads (respects a relocated Downloads folder), unless -OutDir is given.
if (-not $OutDir) {
  $OutDir = $null
  try {
    $reg = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders' -ErrorAction Stop).'{374DE290-123F-4565-9164-39C4925E7B2F}'
    if ($reg) { $OutDir = [Environment]::ExpandEnvironmentVariables($reg) }
  } catch { }
  if (-not $OutDir) { $OutDir = Join-Path $env:USERPROFILE 'Downloads' }
}
if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path

# Credentials, player data, logs and browser profiles. Skipped unless -IncludeSecrets.
$blocked = '(^|/)state/|\.pem$|logins|admin_hmac|requests\.log|/Cache/|/Awesomium/|/LauncherData/|/Log/|Cookies|HardwareInfo|\.log$'
$textExt = @('.json','.csv','.js','.html','.css','.py','.md','.txt','.ps1','.sh','.cmd','.tsc','.service','.timer','.orig','.jsonl','.svg','.ini','.xml','.yml','.yaml')

Write-Host "Scanning $rootFull ..."
Write-Host "Output folder: $OutDir"
$candidates   = New-Object System.Collections.Generic.List[object]
$skipSecret   = New-Object System.Collections.Generic.List[string]
$skipBig      = New-Object System.Collections.Generic.List[string]

foreach ($f in (Get-ChildItem -LiteralPath $rootFull -Recurse -File -Force)) {
  $rel = $f.FullName.Substring($rootFull.Length + 1) -replace '\\', '/'
  if ($rel -match '\.zip$') { continue }
  if (-not $IncludeSecrets -and $rel -match $blocked) { $skipSecret.Add($rel); continue }
  if ($f.Length -ge $maxBytes) { $skipBig.Add($rel); continue }
  $candidates.Add([pscustomobject]@{
    Rel = $rel; Path = $f.FullName; Len = $f.Length; Ext = $f.Extension.ToLower()
  })
}
Write-Host ("{0} files eligible, {1} skipped as sensitive, {2} skipped for size" -f $candidates.Count, $skipSecret.Count, $skipBig.Count)

function New-ZipFromList($zipPath, $files) {
  if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
  $manifest = New-Object System.Text.StringBuilder
  $failed   = New-Object System.Collections.Generic.List[string]
  $n = 0; $bytes = [int64]0
  $zip = [System.IO.Compression.ZipFile]::Open($zipPath, 'Create')
  try {
    foreach ($f in $files) {
      $level = if ($textExt -contains $f.Ext) { 'Optimal' } else { 'Fastest' }
      try {
        [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $f.Path, $f.Rel, $level)
        [void]$manifest.AppendLine(('{0,12:N0} B  {1}' -f $f.Len, $f.Rel))
        $n++; $bytes += $f.Len
        if ($n % 1000 -eq 0) { Write-Host ("  {0} files ..." -f $n) }
      } catch {
        $failed.Add(('{0}  ({1})' -f $f.Rel, $_.Exception.Message))
      }
    }
    $head = ("{0} files, {1:N1} MB uncompressed. Sensitive skipped: {2}. Over size cap skipped: {3}. Failed: {4}.`r`n" -f $n, ($bytes / 1MB), $skipSecret.Count, $skipBig.Count, $failed.Count)
    $tail = New-Object System.Text.StringBuilder
    if ($skipSecret.Count -gt 0) { [void]$tail.AppendLine("`r`n# Skipped (sensitive)"); $skipSecret | ForEach-Object { [void]$tail.AppendLine($_) } }
    if ($skipBig.Count -gt 0)    { [void]$tail.AppendLine("`r`n# Skipped (size)");      $skipBig    | ForEach-Object { [void]$tail.AppendLine($_) } }
    if ($failed.Count -gt 0)     { [void]$tail.AppendLine("`r`n# Failed to read");      $failed     | ForEach-Object { [void]$tail.AppendLine($_) } }
    $entry  = $zip.CreateEntry('_MANIFEST.txt')
    $writer = New-Object System.IO.StreamWriter($entry.Open())
    $writer.Write($head + $manifest.ToString() + $tail.ToString())
    $writer.Dispose()
  } finally { $zip.Dispose() }
  Write-Host ("Created {0}: {1} files, {2:N1} MB zipped" -f $zipPath, $n, ((Get-Item -LiteralPath $zipPath).Length / 1MB))
  if ($failed.Count -gt 0) { Write-Host ("  {0} files could not be read (listed in _MANIFEST.txt)" -f $failed.Count) -ForegroundColor Yellow }
}

if ($Mode -in 'Both','Readable') {
  $readable = $candidates | Where-Object { $textExt -contains $_.Ext }
  New-ZipFromList (Join-Path $OutDir 'mqel-readable.zip') $readable
}
if ($Mode -in 'Both','Full') {
  New-ZipFromList (Join-Path $OutDir 'mqel-full.zip') $candidates
}
