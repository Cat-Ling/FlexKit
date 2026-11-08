$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

$VenvDir = if ($env:VENV_DIR) { $env:VENV_DIR } else { ".venv-build" }
if (-not (Test-Path $VenvDir)) {
    python -m venv $VenvDir
}

& "$VenvDir\Scripts\python.exe" -m pip install --upgrade pip
& "$VenvDir\Scripts\python.exe" -m pip install -r requirements-dev.txt
& "$VenvDir\Scripts\python.exe" -m pytest -q
& "$VenvDir\Scripts\python.exe" scripts/build.py @args
