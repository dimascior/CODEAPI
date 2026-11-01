#!/usr/bin/env pwsh
# scripts/search.ps1 — repo-local source search that works in a CODEAPI-created
# PowerShell terminal even when ripgrep (rg) is NOT on PATH.
#
# Prefers rg; otherwise falls back to Select-String and prints that it did so.
# Excludes generated/vendored trees (.git, node_modules, bin, __pycache__, build,
# dist) either way, so it never drowns in node_modules or the PyInstaller onedir.
#
#   pwsh scripts/search.ps1 <pattern> [path]
#   pwsh scripts/search.ps1 "term_routes" python
#
# Output format matches `rg -n`:  <path>:<line>:<match>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)] [string] $Pattern,
    [Parameter(Position = 1)] [string] $Path = "."
)

$excludeDirs = @('.git', 'node_modules', 'bin', '__pycache__', 'build', 'dist')

if (Get-Command rg -ErrorAction SilentlyContinue) {
    $globs = @()
    foreach ($d in $excludeDirs) { $globs += @('-g', "!$d/**") }
    & rg -n @globs -- $Pattern $Path
}
else {
    Write-Host "[search] ripgrep (rg) not found on PATH - falling back to Select-String." -ForegroundColor Yellow
    Get-ChildItem -Path $Path -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object {
            $full = $_.FullName
            -not ($excludeDirs | Where-Object { $full -match "[\\/]$([regex]::Escape($_))[\\/]" })
        } |
        Select-String -Pattern $Pattern |
        ForEach-Object { "{0}:{1}:{2}" -f $_.Path, $_.LineNumber, $_.Line.TrimEnd() }
}
