#Requires -Version 5.1
<#
.SYNOPSIS
    Packages this Kodi add-on into an installable zip.

.DESCRIPTION
    Reads the add-on id and version straight out of addon.xml, so the zip is
    always named and structured to match the manifest. Entries are written
    with forward slashes and under a single top-level folder named after the
    add-on id, which is what Kodi's "Install from zip file" expects.

.PARAMETER Source
    Folder holding addon.xml. Defaults to the folder this script lives in, or -
    when the script sits one level above the add-on, which is how this repo is
    laid out - the single folder beneath it that holds an addon.xml.

.PARAMETER OutDir
    Where to write the zip. Defaults to a 'dist' folder beside this script.

.PARAMETER Clean
    Delete everything in the output folder before building.

.PARAMETER SkipSyntaxCheck
    Skip the Python syntax check even if python is on PATH.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -OutDir C:\temp -Clean
#>
[CmdletBinding()]
param(
    [string]$Source,
    [string]$OutDir,
    [switch]$Clean,
    [switch]$SkipSyntaxCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.IO.Compression | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null

# Things that must never end up inside the zip.
$excludeDirs = @('__pycache__', '.git', '.github', '.svn', '.vs', '.vscode',
                 '.idea', 'dist', 'build', 'test', 'tests')
$excludeFiles = @('build.ps1', 'build.bat', '*.pyc', '*.pyo', '*.zip',
                  '.gitignore', '.gitattributes', 'Thumbs.db', '.DS_Store',
                  '*.bak', '*.orig', '*~')

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Ok($message)   { Write-Host "    $message" -ForegroundColor Green }
function Write-Warn($message) { Write-Host "    $message" -ForegroundColor Yellow }

# ---------------------------------------------------------------- validate --

$buildRoot = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$buildRoot = $buildRoot.TrimEnd('\')

function Find-AddonRoot($start) {
    # The script may sit inside the add-on folder, or one level above it beside
    # the add-on's own folder, which is how this repo is laid out.
    if (Test-Path -LiteralPath (Join-Path $start 'addon.xml')) { return $start }
    $found = @(Get-ChildItem -LiteralPath $start -Directory -ErrorAction SilentlyContinue |
               Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'addon.xml') })
    if ($found.Count -eq 1) { return $found[0].FullName.TrimEnd('\') }
    if ($found.Count -gt 1) {
        $names = ($found | ForEach-Object { $_.Name }) -join ', '
        throw "Found $($found.Count) add-ons under '$start' ($names). Pass -Source to pick one."
    }
    return $null
}

if ($Source) {
    $sourceRoot = (Resolve-Path -LiteralPath $Source).ProviderPath.TrimEnd('\')
    if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot 'addon.xml'))) {
        throw "No addon.xml found in '$sourceRoot'. Point -Source at the add-on folder."
    }
} else {
    $sourceRoot = Find-AddonRoot $buildRoot
    if (-not $sourceRoot) {
        throw "No addon.xml in '$buildRoot' or in any folder directly beneath it. Point -Source at the add-on folder."
    }
}

$addonXmlPath = Join-Path $sourceRoot 'addon.xml'

Write-Step "Reading addon.xml"
try {
    [xml]$addonXml = Get-Content -LiteralPath $addonXmlPath -Raw -Encoding UTF8
} catch {
    throw "addon.xml is not valid XML: $($_.Exception.Message)"
}

$addonId = $addonXml.addon.id
$addonVersion = $addonXml.addon.version
$addonName = $addonXml.addon.name

if ([string]::IsNullOrWhiteSpace($addonId))      { throw "addon.xml has no id attribute." }
if ([string]::IsNullOrWhiteSpace($addonVersion)) { throw "addon.xml has no version attribute." }
if ($addonVersion -notmatch '^\d+\.\d+\.\d+') {
    Write-Warn "Version '$addonVersion' is not in major.minor.patch form; Kodi may not upgrade cleanly."
}

Write-Ok "$addonName ($addonId) version $addonVersion"

$folderName = Split-Path $sourceRoot -Leaf
if ($folderName -ne $addonId) {
    # Not fatal: entries are rewritten under the id below. But Kodi wants the
    # installed folder to match, so flag it.
    Write-Warn "Source folder is '$folderName' but the add-on id is '$addonId'."
    Write-Warn "The zip will still be correct, but consider renaming the folder."
}

# --------------------------------------------------------- syntax checking --

$serviceFile = Join-Path $sourceRoot 'service.py'
if (-not $SkipSyntaxCheck -and (Test-Path -LiteralPath $serviceFile)) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
    if ($python) {
        Write-Step "Checking Python syntax"
        # @() so a lone .py file still exposes .Count under Set-StrictMode.
        $pyFiles = @(Get-ChildItem -LiteralPath $sourceRoot -Recurse -File -Filter '*.py' |
                    Where-Object { $_.FullName -notmatch '\\__pycache__\\' })
        foreach ($py in $pyFiles) {
            & $python.Source -m py_compile $py.FullName
            if ($LASTEXITCODE -ne 0) { throw "Syntax error in $($py.Name); build aborted." }
        }
        Write-Ok "$($pyFiles.Count) file(s) compiled cleanly"
    } else {
        Write-Warn "Python not on PATH; skipping the syntax check."
    }
}

# py_compile leaves these behind, and they must not be packaged.
Get-ChildItem -LiteralPath $sourceRoot -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }

# ------------------------------------------------------------- file picker --

Write-Step "Collecting files"

$files = Get-ChildItem -LiteralPath $sourceRoot -Recurse -File | Where-Object {
    $relative = $_.FullName.Substring($sourceRoot.Length).TrimStart('\')
    $segments = $relative.Split('\')
    $dirs = @($segments[0..([Math]::Max($segments.Length - 2, 0))])

    $inExcludedDir = $false
    if ($segments.Length -gt 1) {
        foreach ($segment in $dirs) {
            if ($excludeDirs -contains $segment) { $inExcludedDir = $true; break }
        }
    }

    $isExcludedFile = $false
    foreach ($pattern in $excludeFiles) {
        if ($_.Name -like $pattern) { $isExcludedFile = $true; break }
    }

    -not ($inExcludedDir -or $isExcludedFile)
}

if (-not $files) { throw "No files to package." }

$required = @('addon.xml')
foreach ($needed in $required) {
    if (-not ($files | Where-Object { $_.Name -eq $needed })) {
        throw "$needed is missing from the file list; refusing to build."
    }
}

Write-Ok "$($files.Count) file(s) selected"

# -------------------------------------------------------------- output dir --

if (-not $OutDir) { $OutDir = Join-Path $buildRoot 'dist' }

if ($Clean -and (Test-Path -LiteralPath $OutDir)) {
    Write-Step "Cleaning $OutDir"
    Remove-Item -LiteralPath $OutDir -Recurse -Force
}
if (-not (Test-Path -LiteralPath $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}
$OutDir = (Resolve-Path -LiteralPath $OutDir).ProviderPath.TrimEnd('\')

$zipPath = Join-Path $OutDir "$addonId-$addonVersion.zip"
if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }

# -------------------------------------------------------------------- zip --

Write-Step "Writing $zipPath"

$zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($file in ($files | Sort-Object FullName)) {
        $relative = $file.FullName.Substring($sourceRoot.Length).TrimStart('\')
        # Kodi needs one top-level folder named after the id, and zip entry
        # names must use forward slashes regardless of the host OS.
        $entryName = "$addonId/" + ($relative -replace '\\', '/')
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $zip, $file.FullName, $entryName,
            [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
} finally {
    $zip.Dispose()
}

# ------------------------------------------------------------------ verify --

Write-Step "Verifying archive"

$check = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
try {
    $entries = @($check.Entries | ForEach-Object { $_.FullName })
    $bad = @($entries | Where-Object { $_ -notlike "$addonId/*" -or $_ -like '*\*' })
    if ($bad.Count -gt 0) {
        throw "Malformed entries in the archive: $($bad -join ', ')"
    }
    if (-not ($entries -contains "$addonId/addon.xml")) {
        throw "$addonId/addon.xml is missing from the archive."
    }
    foreach ($entry in ($entries | Sort-Object)) { Write-Host "    $entry" }
    Write-Ok "$($entries.Count) entries, all under $addonId/"
} finally {
    $check.Dispose()
}

$sizeKb = [Math]::Round((Get-Item -LiteralPath $zipPath).Length / 1KB, 1)
Write-Host ""
Write-Host "Built $addonId-$addonVersion.zip ($sizeKb KB)" -ForegroundColor Green
Write-Host "  $zipPath"
Write-Host ""
Write-Host "Install: Kodi > Add-ons > Install from zip file" -ForegroundColor DarkGray
