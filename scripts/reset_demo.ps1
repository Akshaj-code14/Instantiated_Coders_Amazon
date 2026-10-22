# FIX (code freeze, item 5): reset ONLY the demo database so the demo can be
# repeated. Deletes backend/data/campuspilot.db (+ -wal/-shm) and reseeds.
#
#   .\scripts\reset_demo.ps1
#
# Stop the server first (a locked database cannot be deleted on Windows).
# This script never touches code, .env, git history, or any other file.

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$DB   = Join-Path $Root 'backend\data\campuspilot.db'

Write-Host "CampusPilot demo reset"
Write-Host "  deleting: $DB (and -wal / -shm)"

foreach ($f in @($DB, "$DB-wal", "$DB-shm")) {
    if (Test-Path $f) {
        try {
            Remove-Item $f -Force
            Write-Host "  removed  $(Split-Path -Leaf $f)"
        } catch {
            Write-Error "Could not delete $f — is the server still running? Stop uvicorn and rerun."
            exit 1
        }
    } else {
        Write-Host "  absent   $(Split-Path -Leaf $f)"
    }
}

# Prefer the project venv, fall back to whatever `python` is on PATH.
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { $Python = 'python' }

Push-Location (Join-Path $Root 'backend')
try {
    & $Python -m services.seed_data
    if ($LASTEXITCODE -ne 0) { throw "seed exited with $LASTEXITCODE" }
} finally {
    Pop-Location
}

Write-Host "Done. Start the server again: cd backend; python -m uvicorn server:app --host 127.0.0.1 --port 8000"
