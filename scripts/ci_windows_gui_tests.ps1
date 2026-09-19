# Run from the repository root in an interactive Windows shell executor.
$ErrorActionPreference = 'Stop'
# Native failures are checked explicitly, including on Windows PowerShell 5.1.
$PSNativeCommandUseErrorActionPreference = $false
$testExitCode = 1
Start-Transcript -Path 'windows-gui-tests.log' -Force
try {
    if ($env:OS -ne 'Windows_NT') {
        throw 'This job requires a native Windows runner.'
    }
    $vcRuntimePath = Join-Path $env:SystemRoot 'System32\MSVCP140.dll'
    if (-not (Test-Path -LiteralPath $vcRuntimePath)) {
        throw 'Missing Microsoft Visual C++ v14 Redistributable (x64): MSVCP140.dll was not found.'
    }
    $desktopSession = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
    if ($desktopSession -eq 0 -or -not [Environment]::UserInteractive) {
        throw 'Run GitLab Runner in a logged-in desktop session, not as a Windows service.'
    }
    Write-Host "Windows GUI tests: user=$env:USERNAME session=$desktopSession PowerShell=$($PSVersionTable.PSVersion)"
    if ($env:NAGUMIX_GUI_TESTS -ne '1') {
        throw 'NAGUMIX_GUI_TESTS must be 1 so visible tests are not silently skipped.'
    }
    if (-not $env:UV_VERSION) { throw 'UV_VERSION must be configured by CI.' }

    # The standalone installer needs neither Python nor the Windows py launcher.
    if ($env:UV_VERSION -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid UV_VERSION.' }
    $ciToolDirectory = Join-Path (Get-Location) '.ci-tools-windows'
    New-Item -ItemType Directory -Force -Path $ciToolDirectory | Out-Null
    $env:UV_UNMANAGED_INSTALL = $ciToolDirectory
    $ciUvPath = Join-Path $ciToolDirectory 'uv.exe'
    $ciUvVersionPattern = "^uv $([regex]::Escape($env:UV_VERSION))(\s|$)"
    $ciReuseUv = $false
    if (Test-Path -LiteralPath $ciUvPath) {
        $ciCachedVersion = & $ciUvPath --version
        $ciReuseUv = ($LASTEXITCODE -eq 0 -and $ciCachedVersion -match $ciUvVersionPattern)
    }
    if (-not $ciReuseUv) {
        $ciInstallerPath = Join-Path $ciToolDirectory 'install-uv.ps1'
        Invoke-WebRequest -UseBasicParsing -Uri "https://astral.sh/uv/$env:UV_VERSION/install.ps1" -OutFile $ciInstallerPath
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ciInstallerPath
        if ($LASTEXITCODE -ne 0) { throw 'Standalone uv installation failed.' }
    }
    else {
        Write-Host 'Reusing cached pinned uv executable.'
    }
    $ciUvVersion = & $ciUvPath --version
    if ($LASTEXITCODE -ne 0 -or $ciUvVersion -notmatch $ciUvVersionPattern) {
        throw 'Installed uv does not match the pinned CI version.'
    }
    Write-Host $ciUvVersion
    # sync downloads managed Python 3.12 when absent, then installs locked wheels.
    & $ciUvPath sync --locked --no-dev --no-install-project --python 3.12
    if ($LASTEXITCODE -ne 0) { throw 'Locked Windows dependency installation failed.' }
    & ./.venv-ci-windows/Scripts/python.exe -c "import platform, sys, wx; from PIL import Image; print(platform.platform(), sys.version, wx.version(), 'Pillow', Image.__version__); assert sys.platform == 'win32'; assert wx.App.IsDisplayAvailable(), 'No wx display available'"
    if ($LASTEXITCODE -ne 0) { throw 'Windows/wx display preflight failed.' }

    # Discover the complete suite, including future regression modules and Windows/GUI gates.
    & ./.venv-ci-windows/Scripts/python.exe run_tests.py -v
    $testExitCode = $LASTEXITCODE
}
finally {
    Stop-Transcript
}
exit $testExitCode
