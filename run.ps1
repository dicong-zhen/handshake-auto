# Launches the Screen AI Assistant.
# Creates a virtual environment and installs dependencies on first run.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    & $python -m venv .venv
    & ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
    & ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
}

$env:PYTHONUTF8 = "1"
$ErrorActionPreference = "Continue"   # don't stop on Python stderr output

# Tcl/Tk intermittently panics with "Failed to create the menu window" when it
# initialises its internal Windows menu subsystem during Tk() startup over
# RDP / AnyDesk (the remote window-station isn't always ready). It's a native
# abort, so Python can't catch it — the process dies and we must relaunch it.
# This is usually cleared within a few seconds, so we retry persistently with a
# short progressive backoff.
#
# Exit code 0   = user closed the window normally           → stop
# Exit code > 0 = Python/KeyboardInterrupt (normal Ctrl-C)  → stop UNLESS menu crash
# Exit code < 0 = native STATUS_BREAKPOINT / STATUS_*       → always retry
$maxAttempts = 25
$succeeded = $false
for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    $menuCrash = $false

    # Pipe through ForEach-Object so we can display output in real-time
    # AND detect the Tcl menu-window initialisation failure.
    & ".\.venv\Scripts\python.exe" -X utf8 main.py 2>&1 | ForEach-Object {
        Write-Host $_
        if ($_ -match "Failed to create the menu window") {
            $script:menuCrash = $true
        }
    }
    $code = $LASTEXITCODE

    if ($code -ge 0 -and -not $menuCrash) {
        $succeeded = $true
        break   # normal exit or Python exception unrelated to menu crash
    }

    # Progressive backoff: quick early retries (the transient often clears
    # almost immediately), easing off to a few seconds so we don't hammer a
    # session that needs longer to become interactive.
    $delay = [Math]::Min(1 + [Math]::Floor($attempt / 2), 5)
    if ($menuCrash) {
        Write-Host "Tcl menu-window init failed (attempt $attempt/$maxAttempts); retrying in ${delay}s..."
    } else {
        Write-Host "Native crash (code $code) on attempt $attempt/$maxAttempts; retrying in ${delay}s..."
    }
    Start-Sleep -Seconds $delay
}

if (-not $succeeded) {
    Write-Host ""
    Write-Host "The app could not start after $maxAttempts attempts." -ForegroundColor Yellow
    Write-Host "Tk kept failing to create its menu window. This usually means the" -ForegroundColor Yellow
    Write-Host "remote desktop session is not fully interactive yet. Try:" -ForegroundColor Yellow
    Write-Host "  - Make sure the RDP/AnyDesk window is connected and in the foreground," -ForegroundColor Yellow
    Write-Host "  - Wait a few seconds after connecting, then run .\run.ps1 again." -ForegroundColor Yellow
}
