$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host ("== " + $Message) -ForegroundColor Green
}

function Test-PythonCommand {
    param(
        [string]$Exe,
        [string[]]$PrefixArgs
    )
    & $Exe @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" *> $null
    return ($LASTEXITCODE -eq 0)
}

$PythonExe = $null
$PythonPrefixArgs = @()

if (Get-Command py -ErrorAction SilentlyContinue) {
    if (Test-PythonCommand -Exe "py" -PrefixArgs @("-3")) {
        $PythonExe = "py"
        $PythonPrefixArgs = @("-3")
    }
}

if (-not $PythonExe) {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        if (Test-PythonCommand -Exe "python" -PrefixArgs @()) {
            $PythonExe = "python"
            $PythonPrefixArgs = @()
        }
    }
}

if (-not $PythonExe) {
    if (Get-Command python3 -ErrorAction SilentlyContinue) {
        if (Test-PythonCommand -Exe "python3" -PrefixArgs @()) {
            $PythonExe = "python3"
            $PythonPrefixArgs = @()
        }
    }
}

if (-not $PythonExe) {
    Write-Host ""
    Write-Host "Python 3.10+ was not found." -ForegroundColor Red
    Write-Host "Please install Python 3.12 x64, then run Start_Windows.bat again."
    Write-Host "Download: https://www.python.org/downloads/windows/"
    exit 1
}

Write-Host "Factory Fee Console - Windows Launcher" -ForegroundColor Green
Write-Host ("Project folder: " + $Root)

Write-Step "Check Python"
$VersionArgs = @("-c", "import sys; print('Python', sys.version.split()[0])")
& $PythonExe @PythonPrefixArgs @VersionArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "Python check failed." -ForegroundColor Red
    exit 1
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Step "Create local Python environment"
    $VenvArgs = @("-m", "venv", ".venv")
    & $PythonExe @PythonPrefixArgs @VenvArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Failed to create .venv." -ForegroundColor Red
        exit 1
    }
}

Write-Step "Install dependencies"
$Wheelhouse = Join-Path $Root "wheelhouse"
$RuntimeRequirements = Join-Path $Root "requirements-runtime.txt"
if ((Test-Path $Wheelhouse) -and (Test-Path $RuntimeRequirements)) {
    Write-Host "Offline wheelhouse found. Installing dependencies from local files."
    & $VenvPython -c "import sys; print('Dependency target: cp%d%d' % (sys.version_info.major, sys.version_info.minor))"
    if ($LASTEXITCODE -ne 0) {
        exit 1
    }
    & $VenvPython -m pip install --no-index --find-links "$Wheelhouse" -r "$RuntimeRequirements"
} else {
    Write-Host "No wheelhouse found. Installing dependencies from internet."
    & $VenvPython -m pip install -r requirements.txt
}
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Dependency installation failed." -ForegroundColor Red
    Write-Host "Use the offline package with wheelhouse, or ask IT to allow pip access."
    exit 1
}

Write-Step "Start local console"
Write-Host "URL: http://127.0.0.1:5050"
Write-Host "Keep this window open. Closing it will stop the console."
Write-Host ""

Start-Process "http://127.0.0.1:5050"
& $VenvPython scripts\run_web.py --host 127.0.0.1 --port 5050
