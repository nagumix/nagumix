[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

if (-not [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
        [System.Runtime.InteropServices.OSPlatform]::Windows)) {
    throw 'This build script requires Windows.'
}
if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne 'X64') {
    throw 'This build definition currently supports Windows x64 only.'
}

$previousEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    $env:UV_PROJECT_ENVIRONMENT = '.venv-packaging'
    uv sync --locked --group packaging --no-default-groups
    if ($LASTEXITCODE -ne 0) { throw 'uv sync failed.' }

    & '.\.venv-packaging\Scripts\python.exe' 'scripts\write_build_metadata.py'
    if ($LASTEXITCODE -ne 0) { throw 'Build metadata generation failed.' }

    & '.\.venv-packaging\Scripts\python.exe' -m PyInstaller `
        --noconfirm --clean 'packaging\nagumix-windows.spec'
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

    $zip = 'dist\NaguMIX-0.1.0-windows-x64.zip'
    if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip }
    Compress-Archive -LiteralPath 'dist\NaguMIX' -DestinationPath $zip -CompressionLevel Optimal

    Get-Item -LiteralPath 'dist\NaguMIX\NaguMIX.exe', $zip |
        Select-Object FullName, Length, @{Name='SHA256'; Expression={(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash}}
}
finally {
    $env:UV_PROJECT_ENVIRONMENT = $previousEnvironment
}
