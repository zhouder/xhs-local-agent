param([switch]$Check)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        & py -3.12 -m venv .venv
    } else {
        $python = Get-Command python -ErrorAction SilentlyContinue
        if (-not $python) { throw "Python 3.12 is required. Install it from python.org first." }
        & python -m venv .venv
    }
}

$requirementsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath requirements.txt).Hash
$stampPath = Join-Path $PSScriptRoot ".venv\requirements.sha256"
$installedHash = if (Test-Path -LiteralPath $stampPath) { (Get-Content -Raw -LiteralPath $stampPath).Trim() } else { "" }
if ($installedHash -ne $requirementsHash) {
    & $venvPython -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed with exit code $LASTEXITCODE." }
    Set-Content -LiteralPath $stampPath -Value $requirementsHash -Encoding ASCII
}

if ($Check) {
    & $venvPython -c "from app.main import app; from app.database import init_db; init_db(); print('Environment check OK')"
    exit $LASTEXITCODE
}

& $venvPython -m app.cli
