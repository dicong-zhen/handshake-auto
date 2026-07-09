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

# Retry up to 5 times when Tcl/Tk fails to initialise the menu subsystem
# (a transient RDP/Windows issue).
# Exit code 0   = user closed the window normally           → stop
# Exit code > 0 = Python/KeyboardInterrupt (normal Ctrl-C)  → stop UNLESS menu crash
# Exit code < 0 = native STATUS_BREAKPOINT / STATUS_*       → always retry
for ($attempt = 1; $attempt -le 5; $attempt++) {
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
        break   # normal exit or Python exception unrelated to menu crash
    }

    if ($menuCrash) {
        Write-Host "Tcl menu-window init failed on attempt $attempt, retrying in 2s..."
    } else {
        Write-Host "Native crash (code $code) on attempt $attempt, retrying in 2s..."
    }
    Start-Sleep -Seconds 2
}
