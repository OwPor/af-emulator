#requires -Version 5.1
<#
Assault Fire PH v1.0.0.24 - one-click setup + launcher.

Recommended layout:

    AssaultFirePH\
    |-- Binaries\Win32\TGame.exe
    |-- TCLS\client.exe
    |-- TCLS\Tenio\TCLS.dll
    |-- TGame\...
    |-- af-emulator\
        |-- START_ASSAULT_FIRE.ps1

The repository contents may also be copied directly into the game root.

This script does NOT download or redistribute Assault Fire game files.
Before launch, the verified datetime patch is applied to the user's TGame.exe
with an exact TGame.exe.bak backup. TGame_AFDEV.exe is then made as a local
private copy and receives the verified native ServerMove-v4 AFDEV patch.
#>

[CmdletBinding()]
param(
    [switch]$SetupOnly,
    [switch]$SkipPythonInstall,
    [switch]$KeepServer
)

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

$currentPowerShellExe = if ($PSVersionTable.PSEdition -eq "Core") {
    Join-Path $PSHOME "pwsh.exe"
} else {
    Join-Path $PSHOME "powershell.exe"
}
if (-not (Test-Path -LiteralPath $currentPowerShellExe -PathType Leaf)) {
    $currentPowerShellExe = (Get-Process -Id $PID).Path
}
if (-not $currentPowerShellExe -or -not (Test-Path -LiteralPath $currentPowerShellExe -PathType Leaf)) {
    Write-Host "[AF-ONECLICK] ERROR: Could not locate the current PowerShell executable."
    Read-Host "Press Enter to close"
    exit 1
}

if (-not (Test-IsAdministrator)) {
    $launcherPath = $MyInvocation.MyCommand.Path
    if (-not $launcherPath) {
        Write-Host "[AF-ONECLICK] ERROR: Could not determine the launcher script path."
        Read-Host "Press Enter to close"
        exit 1
    }
    $launcherPath = (Resolve-Path -LiteralPath $launcherPath).Path
    $elevatedArguments = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        ('"' + $launcherPath + '"')
    )
    if ($SetupOnly) { $elevatedArguments += "-SetupOnly" }
    if ($SkipPythonInstall) { $elevatedArguments += "-SkipPythonInstall" }
    if ($KeepServer) { $elevatedArguments += "-KeepServer" }

    Write-Host "[UAC] Requesting Administrator access for the one-click launcher..."
    try {
        Start-Process -FilePath $currentPowerShellExe -Verb RunAs -WorkingDirectory (Split-Path -Parent $launcherPath) -ArgumentList $elevatedArguments | Out-Null
    } catch {
        Write-Host "[AF-ONECLICK] ERROR: Administrator launch was cancelled or failed: $($_.Exception.Message)" -ForegroundColor Red
        Read-Host "Press Enter to close"
        exit 1
    }
    exit 0
}

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$LAUNCHER_REVISION = "2026-10-05-oneclick-v36-servermove-v4"
$EXPECTED_TGAME_SHA256 = "B4273F2658CA94EEBC559A997FDFCD02D51E77CE75B892250C1DB7FB80C70B51"
$TCLS_ORIGINAL_SHA256 = "13EAD403452E0F25CF00658369BF4BF5FF34ED1B16027F7833FB27D398386CD1"
$TCLS_PATCHED_SHA256  = "3FF351E0ADB594D7544E28DB2E966A6D6EB548E9DF70DAAF4DAF58F2EE438D56"

$script:LAUNCHER_CONFIG_PATH = Join-Path $PSScriptRoot "launcher.config.json"
$script:LauncherConfig = $null

function New-DefaultLauncherConfig {
    return [pscustomobject]@{
        Version = 1
        Preferences = [pscustomobject]@{}
    }
}

function Save-LauncherConfig {
    $temporaryPath = $script:LAUNCHER_CONFIG_PATH + ".tmp"
    try {
        $script:LauncherConfig |
            ConvertTo-Json -Depth 8 |
            Set-Content -LiteralPath $temporaryPath -Encoding UTF8
        Move-Item -LiteralPath $temporaryPath -Destination $script:LAUNCHER_CONFIG_PATH -Force | Out-Null
    } catch {
        Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        Write-Host "[WARNING] Could not save launcher preferences: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

function Initialize-LauncherConfig {
    $script:LauncherConfig = New-DefaultLauncherConfig
    if (-not (Test-Path -LiteralPath $script:LAUNCHER_CONFIG_PATH -PathType Leaf)) {
        return
    }

    try {
        $loaded = Get-Content -LiteralPath $script:LAUNCHER_CONFIG_PATH -Raw -Encoding UTF8 |
            ConvertFrom-Json
        if ($null -eq $loaded -or [int]$loaded.Version -ne 1) {
            throw "Unsupported or missing config version."
        }
        if ($null -eq $loaded.PSObject.Properties["Preferences"] -or $null -eq $loaded.Preferences) {
            $loaded | Add-Member -MemberType NoteProperty -Name Preferences -Value ([pscustomobject]@{}) -Force
        }
        if ($loaded.Preferences -isnot [pscustomobject]) {
            throw "Preferences must be a JSON object."
        }
        $script:LauncherConfig = $loaded
    } catch {
        $backupPath = $script:LAUNCHER_CONFIG_PATH + ".invalid." + (Get-Date -Format "yyyyMMdd_HHmmss") + ".bak"
        Copy-Item -LiteralPath $script:LAUNCHER_CONFIG_PATH -Destination $backupPath -Force
        Write-Host "[WARNING] Launcher config could not be read. A backup was saved to $backupPath; defaults will be used." -ForegroundColor Yellow
        $script:LauncherConfig = New-DefaultLauncherConfig
    }
}

function Set-LauncherPreference([string]$Name, [bool]$Value) {
    $savedValue = if ($Value) { "Y" } else { "N" }
    $script:LauncherConfig.Preferences |
        Add-Member -MemberType NoteProperty -Name $Name -Value $savedValue -Force
    Save-LauncherConfig
}

function Get-LauncherPreference([string]$Name) {
    $property = $script:LauncherConfig.Preferences.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }

    $value = $property.Value
    if ($value -is [bool]) {
        return $value
    }
    if ([string]$value -match "^(?i)y(es)?$") {
        return $true
    }
    if ([string]$value -match "^(?i)n(o)?$") {
        return $false
    }
    return $null
}

function Read-LauncherChoice(
    [string]$Name,
    [string]$Prompt,
    [bool]$DefaultYes,
    [switch]$RememberYesOnly,
    [switch]$DoNotRemember
) {
    $savedChoice = $null
    if (-not $DoNotRemember) {
        $savedChoice = Get-LauncherPreference $Name
    }
    if ($null -ne $savedChoice) {
        $label = if ($savedChoice) { "Y" } else { "N" }
        Write-Host ("[CONFIG] Saved choice for {0}: {1}" -f $Name, $label) -ForegroundColor DarkGray
        return [bool]$savedChoice
    }

    $suffix = if ($DefaultYes) { "[Y/n]" } else { "[y/N]" }
    while ($true) {
        $answer = Read-Host "$Prompt $suffix"
        if ([string]::IsNullOrWhiteSpace($answer)) {
            $choice = $DefaultYes
        } elseif ($answer -match "^(?i)y(es)?$") {
            $choice = $true
        } elseif ($answer -match "^(?i)n(o)?$") {
            $choice = $false
        } else {
            Write-Host "Please enter Y or N, or press Enter to use the default." -ForegroundColor Yellow
            continue
        }

        if (-not $DoNotRemember -and (-not $RememberYesOnly -or $choice)) {
            Set-LauncherPreference $Name $choice
        }
        return [bool]$choice
    }
}

function Write-Title([string]$Text) {
    Write-Host ""
    Write-Host ("=" * 72) -ForegroundColor Cyan
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ("=" * 72) -ForegroundColor Cyan
}

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "[AF-ONECLICK] $Text" -ForegroundColor Yellow
}

function Stop-WithMessage([string]$Message) {
    Write-Host ""
    Write-Host "[AF-ONECLICK] ERROR: $Message" -ForegroundColor Red
    Write-Host ""
    Write-Host "Nothing else will be launched. Fix the message above, then run this script again."
    Read-Host "Press Enter to close"
    exit 1
}

function Quote-PS([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

function ConvertTo-PowerShellEncodedCommand([string]$Command) {
    $bytes = [System.Text.Encoding]::Unicode.GetBytes($Command)
    return [Convert]::ToBase64String($bytes)
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Backup-IfExists([string]$Path, [string]$Reason) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $backup = "$Path.$Reason.$stamp.bak"
    Copy-Item -LiteralPath $Path -Destination $backup -Force
    Write-Host "[BACKUP] $Path"
    Write-Host "      -> $backup"
}

function Invoke-Checked([string]$Exe, [string[]]$Arguments, [string]$Description) {
    Write-Host "[RUN] $Description"

    $savedErrorActionPreference = $ErrorActionPreference
    $PSNativeCommandUseErrorActionPreference = $false
    $global:LASTEXITCODE = 1
    try {
        $ErrorActionPreference = "Continue"
        & $Exe @Arguments 2>&1 | ForEach-Object {
            Write-Host ([string]$_)
        }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedErrorActionPreference
    }

    if ($exitCode -ne 0) {
        throw "$Description failed with exit code $exitCode"
    }
}

function Find-GameRoot([string]$RepoRoot) {
    $candidates = New-Object System.Collections.Generic.List[string]
    $candidates.Add($RepoRoot)

    $parent = Split-Path -Parent $RepoRoot
    if ($parent) { $candidates.Add($parent) }

    $grand = if ($parent) { Split-Path -Parent $parent } else { $null }
    if ($grand) { $candidates.Add($grand) }

    foreach ($candidate in $candidates) {
        $client = Join-Path $candidate "TCLS\client.exe"
        $tcls = Join-Path $candidate "TCLS\Tenio\TCLS.dll"
        $tgame = Join-Path $candidate "Binaries\Win32\TGame.exe"
        if (
            (Test-Path -LiteralPath $client -PathType Leaf) -and
            (Test-Path -LiteralPath $tcls -PathType Leaf) -and
            (Test-Path -LiteralPath $tgame -PathType Leaf)
        ) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Test-SupportedPythonPath([string]$Candidate) {
    if (-not $Candidate) { return $null }
    try {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) { $exe = (Resolve-Path -LiteralPath $Candidate).Path }
        else { $command = Get-Command $Candidate -ErrorAction SilentlyContinue; $exe = if ($command) { $command.Source } else { $null } }
        if (-not $exe -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) { return $null }
        $probeLines = @(& $exe -c "import sys; print(('OK|' if sys.version_info >= (3,10) and sys.version_info < (4,0) else 'NO|') + sys.executable)" 2>$null)
        $probeExitCode = $LASTEXITCODE
        $probe = $probeLines | Select-Object -First 1
        if ($probeExitCode -ne 0 -or -not $probe) { return $null }
        $parts = ([string]$probe).Trim() -split "\|", 2
        if ($parts.Count -ne 2 -or $parts[0] -ne "OK") { return $null }
        $resolved = $parts[1].Trim()
        if ($resolved -and (Test-Path -LiteralPath $resolved -PathType Leaf)) { return (Resolve-Path -LiteralPath $resolved).Path }
        return (Resolve-Path -LiteralPath $exe).Path
    } catch { return $null }
}

function Find-VenvPython([string]$VenvDir) {
    if (-not $VenvDir -or -not (Test-Path -LiteralPath $VenvDir -PathType Container)) { return $null }
    $scripts = Join-Path $VenvDir "Scripts"
    if (-not (Test-Path -LiteralPath $scripts -PathType Container)) { return $null }
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($name in @("python.exe", "python3.exe")) {
        $path = Join-Path $scripts $name
        if (Test-Path -LiteralPath $path -PathType Leaf) { $candidates.Add($path) }
    }
    Get-ChildItem -LiteralPath $scripts -Filter "python*.exe" -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notmatch "(?i)^pythonw" } |
        Sort-Object Name |
        ForEach-Object { $candidates.Add($_.FullName) }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        $found = Test-SupportedPythonPath $candidate
        if ($found) { return $found }
    }
    return $null
}

function Find-PythonInstallManager {
    foreach ($name in @("pymanager.exe", "pymanager")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and $command.Source -and (Test-Path -LiteralPath $command.Source -PathType Leaf)) { return (Resolve-Path -LiteralPath $command.Source).Path }
    }
    if ($env:LOCALAPPDATA) {
        $candidate = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\pymanager.exe"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return (Resolve-Path -LiteralPath $candidate).Path }
    }
    return $null
}

function Find-ManagedPython {
    $manager = Find-PythonInstallManager
    if (-not $manager) { return $null }
    try {
        $rows = @(& $manager list --format=exe --one 3 2>$null)
        if ($LASTEXITCODE -eq 0) {
            foreach ($row in $rows) {
                $candidate = ([string]$row).Trim().Trim('"')
                if (-not $candidate) { continue }
                $found = Test-SupportedPythonPath $candidate
                if ($found) { return $found }
            }
        }
    } catch {}
    return $null
}

function Add-PythonManagerAliasesToPath {
    if (-not $env:LOCALAPPDATA) { return }
    $windowsApps = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps"
    if (-not (Test-Path -LiteralPath $windowsApps -PathType Container)) { return }
    $pathEntries = @()
    if ($env:Path) { $pathEntries = @($env:Path -split [regex]::Escape([System.IO.Path]::PathSeparator)) }
    foreach ($entry in $pathEntries) {
        if ([string]$entry -and ([string]$entry).TrimEnd('\') -ieq $windowsApps.TrimEnd('\')) { return }
    }
    if ($env:Path) { $env:Path = $windowsApps + [System.IO.Path]::PathSeparator + $env:Path }
    else {
        $env:Path = $windowsApps
    }
}

function Resolve-SupportedPython([string]$RepoRoot = "", [string]$GameRoot = "") {
    if ($GameRoot) {
        $runtimeRoot = Join-Path $GameRoot ".af-emulator-runtime"
        foreach ($name in @("venv", "venv-py312")) {
            $found = Find-VenvPython (Join-Path $runtimeRoot $name)
            if ($found) { return $found }
        }
    }
    if ($RepoRoot) {
        $found = Find-VenvPython (Join-Path $RepoRoot ".venv")
        if ($found) { return $found }
    }
    foreach ($name in @("python.exe","python","python3.exe","python3","python314.exe","python314","python3.14.exe","python3.14","python313.exe","python313","python3.13.exe","python3.13","python312.exe","python312","python3.12.exe","python3.12","python311.exe","python311","python3.11.exe","python3.11","python310.exe","python310","python3.10.exe","python3.10")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { $found = Test-SupportedPythonPath $command.Source; if ($found) { return $found } }
    }
    $pyLauncher = $null
    foreach ($pyName in @("py.exe", "py")) {
        $candidateCommand = Get-Command $pyName -ErrorAction SilentlyContinue
        if ($candidateCommand) { $pyLauncher = $candidateCommand; break }
    }
    if ($pyLauncher) {
        $found = Test-SupportedPythonPath $pyLauncher.Source
        if ($found) { Write-Host "[PY-DETECT] Python Launcher default is supported: $found"; return $found }
        foreach ($minor in @(14, 13, 12, 11, 10)) {
            foreach ($selector in @("-3.$minor", "-V:3.$minor")) {
                try {
                    $resolvedLines = @(& $pyLauncher.Source $selector -c "import sys; print(sys.executable)" 2>$null)
                    $resolveExitCode = $LASTEXITCODE; $resolved = $resolvedLines | Select-Object -First 1
                    if ($resolveExitCode -eq 0 -and $resolved) { $found = Test-SupportedPythonPath $resolved.Trim(); if ($found) { return $found } }
                } catch {}
            }
        }
        foreach ($listArgs in @(@("-0p"), @("--list-paths"))) {
            try {
                $rows = @(& $pyLauncher.Source @listArgs 2>$null)
                foreach ($row in $rows) {
                    $text = [string]$row
                    $match = [regex]::Match($text,'([A-Za-z]:\\[^\r\n]*?python(?:3(?:\.\d+)?)?\.exe)',[System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
                    if ($match.Success) { $found = Test-SupportedPythonPath $match.Groups[1].Value.Trim(); if ($found) { return $found } }
                }
            } catch {}
        }
    }
    $managedPython = Find-ManagedPython
    if ($managedPython) { return $managedPython }
    $roots = New-Object System.Collections.Generic.List[string]
    if ($env:LOCALAPPDATA) { $roots.Add((Join-Path $env:LOCALAPPDATA "Programs\Python")) }
    if ($env:ProgramFiles) { $roots.Add($env:ProgramFiles) }
    $programFilesX86 = [Environment]::GetFolderPath("ProgramFilesX86")
    if ($programFilesX86) { $roots.Add($programFilesX86) }
    $systemDrive = if ($env:SystemDrive) { $env:SystemDrive } else { "C:" }
    foreach ($dir in @(Get-ChildItem -LiteralPath ($systemDrive + "\") -Directory -Filter "Python*" -ErrorAction SilentlyContinue)) {
        foreach ($pythonFile in @(Get-ChildItem -LiteralPath $dir.FullName -Filter "python*.exe" -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -notmatch "(?i)^pythonw" })) {
            $found = Test-SupportedPythonPath $pythonFile.FullName; if ($found) { return $found }
        }
    }
    foreach ($root in ($roots | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        $dirs = @()
        if ((Split-Path -Leaf $root) -eq "Python") { $dirs = @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue) }
        else { $dirs = @(Get-ChildItem -LiteralPath $root -Directory -Filter "Python*" -ErrorAction SilentlyContinue) }
        foreach ($dir in $dirs) {
            $pythonFiles = @(Get-ChildItem -LiteralPath $dir.FullName -Filter "python*.exe" -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -notmatch "(?i)^pythonw" })
            foreach ($pythonFile in $pythonFiles) { $found = Test-SupportedPythonPath $pythonFile.FullName; if ($found) { return $found } }
        }
    }
    return $null
}

function Ensure-SupportedPython([string]$RepoRoot, [string]$GameRoot) {
    $python = Resolve-SupportedPython $RepoRoot $GameRoot
    if ($python) { $versionText = Get-PythonVersionText $python; Write-Host "[OK] Supported Python: $versionText  $python" -ForegroundColor Green; return $python }
    if ($SkipPythonInstall) { throw "No supported Python was found. Install Python 3.10 or newer, then run this script again." }
    Write-Host "[SETUP] No supported Python 3.10+ runtime was found."
    Write-Host "[SETUP] Trying Windows Package Manager (winget) to install Python Install Manager because no usable local Python was detected."
    Add-PythonManagerAliasesToPath
    $manager = Find-PythonInstallManager
    if (-not $manager) {
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if ($winget) {
            $managerPackageId = "9NQ7512CXL7T"
            Write-Host "[SETUP] Installing Python Install Manager through Windows Package Manager..."
            $wingetExit = 1
            try { & $winget.Source install --exact --id $managerPackageId --accept-package-agreements --accept-source-agreements --disable-interactivity 2>&1 | ForEach-Object { Write-Host ([string]$_) }; $wingetExit = $LASTEXITCODE }
            catch { Write-Host "[WARNING] winget raised an error while installing Python Install Manager: $($_.Exception.Message)" -ForegroundColor Yellow }
            Add-PythonManagerAliasesToPath; $manager = Find-PythonInstallManager
            if (-not $manager) {
                $python = Resolve-SupportedPython $RepoRoot $GameRoot
                if ($python) { $versionText = Get-PythonVersionText $python; Write-Host "[OK] Supported Python found after winget attempt: $versionText  $python" -ForegroundColor Green; return $python }
                Write-Host "[WARNING] Python Install Manager was not available after the winget attempt (exit $wingetExit)." -ForegroundColor Yellow
            }
        } else { Write-Host "[WARNING] winget is not available on this Windows installation." -ForegroundColor Yellow }
    }
    if ($manager) {
        Write-Host "[SETUP] Installing the current stable Python 3 runtime..."
        $managerExit = 1
        for ($attempt = 1; $attempt -le 2; $attempt++) {
            $managerUpdatedDuringInstall = $false; $managerOutput = New-Object System.Collections.Generic.List[string]
            try { & $manager install default 2>&1 | ForEach-Object { $line=[string]$_; [void]$managerOutput.Add($line); Write-Host $line }; $managerExit=$LASTEXITCODE }
            catch { $message=$_.Exception.Message; Write-Host "[WARNING] Python Install Manager raised an error: $message" -ForegroundColor Yellow; if ($message -match "(?i)Python install manager was successfully updated") { $managerUpdatedDuringInstall=$true } }
            foreach ($line in $managerOutput) { if ($line -match "(?i)Python install manager was successfully updated") { $managerUpdatedDuringInstall=$true } }
            Add-PythonManagerAliasesToPath; $python=Resolve-SupportedPython $RepoRoot $GameRoot
            if ($python) { $versionText=Get-PythonVersionText $python; Write-Host "[OK] Supported Python installed: $versionText  $python" -ForegroundColor Green; return $python }
            if ($attempt -eq 1 -and $managerUpdatedDuringInstall) { Write-Host "[SETUP] Python Install Manager updated itself; retrying runtime installation..."; Start-Sleep -Seconds 1; Add-PythonManagerAliasesToPath; $manager=Find-PythonInstallManager; if ($manager) { continue }; Write-Host "[WARNING] Python Install Manager could not be found after its self-update." -ForegroundColor Yellow }
            break
        }
        Write-Host "[WARNING] Python Install Manager did not provide a usable Python 3.10+ runtime (exit $managerExit)." -ForegroundColor Yellow
    }
    throw "No usable Python 3.10+ interpreter was found. Install any 64-bit Python 3.10+ version or the Python Install Manager, then rerun this launcher."
}

function Get-PythonVersionText([string]$Exe) {
    try { $versionLines=@(& $Exe --version 2>&1); $versionExitCode=$LASTEXITCODE; $line=$versionLines|Select-Object -First 1; if ($versionExitCode -eq 0 -and $line) { return ([string]$line).Trim() } } catch {}
    return ""
}

function Test-VenvDependencies([string]$VenvPython) {
    try { $validationOutput=@(& $VenvPython -c "import cryptography, sys; major = int(cryptography.__version__.split('.', 1)[0]); print('AF_CRYPTOGRAPHY_OK' if 42 <= major < 47 else 'AF_CRYPTOGRAPHY_BAD'); sys.exit(0 if 42 <= major < 47 else 1)" 2>$null); return ($validationOutput -contains "AF_CRYPTOGRAPHY_OK") } catch { return $false }
}

function Preserve-BadRuntime([string]$Path, [string]$Kind) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $stamp=Get-Date -Format "yyyyMMdd_HHmmss"; $parent=Split-Path -Parent $Path; $leaf=Split-Path -Leaf $Path; $backup=Join-Path $parent "$leaf.$Kind.$stamp"
    Move-Item -LiteralPath $Path -Destination $backup
    Write-Host "[REPAIR] Preserved old runtime as: $backup" -ForegroundColor Yellow
}

function Ensure-Venv([string]$RepoRoot, [string]$GameRoot, [string]$BootstrapPython) {
    $requirements=Join-Path $RepoRoot "requirements.txt"
    if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) { throw "requirements.txt is missing from the emulator folder." }
    $runtimeRoot=Join-Path $GameRoot ".af-emulator-runtime"; $venvDir=Join-Path $runtimeRoot "venv"; $legacyPersistentDir=Join-Path $runtimeRoot "venv-py312"
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    $activeDir=$null; $venvPython=$null
    if (Test-Path -LiteralPath $venvDir -PathType Container) { $venvPython=Find-VenvPython $venvDir; if ($venvPython) { $activeDir=$venvDir; Write-Host "[OK] Reusing persistent Python environment: $activeDir" -ForegroundColor Green } else { Preserve-BadRuntime $venvDir "incomplete-or-unsupported" } }
    if (-not $venvPython -and (Test-Path -LiteralPath $legacyPersistentDir -PathType Container)) { $legacyPython=Find-VenvPython $legacyPersistentDir; if ($legacyPython) { $activeDir=$legacyPersistentDir; $venvPython=$legacyPython; Write-Host "[OK] Reusing legacy persistent runtime: $activeDir" -ForegroundColor Green } else { Preserve-BadRuntime $legacyPersistentDir "incomplete-or-unsupported" } }
    if (-not $venvPython) {
        Write-Host "[SETUP] Creating persistent Python environment (FIRST TIME ONLY)..."
        Invoke-Checked -Exe $BootstrapPython -Arguments @("-m", "venv", $venvDir) -Description "create persistent venv"
        $venvPython=Find-VenvPython $venvDir
        if (-not $venvPython) { throw "New persistent Python runtime was created, but no working Python 3.10+ executable could be found under its Scripts folder." }
        $activeDir=$venvDir; Write-Host "[OK] Persistent Python environment created: $venvPython" -ForegroundColor Green
    }
    $marker=Join-Path $activeDir ".af_requirements_sha256"; $wantedHash=Get-Sha256 $requirements; $currentHash=""
    if (Test-Path -LiteralPath $marker -PathType Leaf) { $currentHash=(Get-Content -LiteralPath $marker -Raw).Trim().ToUpperInvariant() }
    if ($currentHash -eq $wantedHash) { Write-Host "[OK] Python dependencies are already installed." -ForegroundColor Green; return $venvPython }
    if (Test-VenvDependencies $venvPython) { Set-Content -LiteralPath $marker -Value $wantedHash -Encoding ASCII; Write-Host "[OK] Existing Python dependencies verified; no pip install needed." -ForegroundColor Green; return $venvPython }
    Write-Host "[SETUP] Installing emulator Python dependency (FIRST TIME OR REQUIREMENTS CHANGED)..."
    Invoke-Checked -Exe $venvPython -Arguments @(
        "-m", "pip", "install",
        "--disable-pip-version-check",
        "-r", $requirements
    ) -Description "install requirements"
    Set-Content -LiteralPath $marker -Value $wantedHash -Encoding ASCII
    if (-not (Test-VenvDependencies $venvPython)) { throw "Python dependencies were installed, but the required cryptography package still failed validation." }
    return $venvPython
}

function Test-AFHosts {
    $hostsPath=Join-Path $env:SystemRoot "System32\drivers\etc\hosts"
    if (-not (Test-Path -LiteralPath $hostsPath -PathType Leaf)) { return $false }
    $wanted=@("tversion.levelupgames.ph","tauthproxy.levelupgames.ph","tdir.levelupgames.ph"); $seen=@{}; foreach($name in $wanted){$seen[$name]=@()}
    foreach($line in Get-Content -LiteralPath $hostsPath){$content=($line -split "#",2)[0].Trim(); if(-not $content){continue}; $parts=$content -split "\s+"; if($parts.Count -lt 2){continue}; $ip=$parts[0]; foreach($rawName in $parts[1..($parts.Count-1)]){$name=$rawName.ToLowerInvariant(); if($seen.ContainsKey($name)){$seen[$name]+=$ip}}}
    foreach($name in $wanted){$values=@($seen[$name]); if($values.Count -ne 1 -or $values[0] -ne "127.0.0.1"){return $false}}
    return $true
}

function Ensure-Hosts([string]$RepoRoot) {
    if (Test-AFHosts){Write-Host "[OK] Windows hosts mappings already point to 127.0.0.1." -ForegroundColor Green; return}
    $hostScript=Join-Path $RepoRoot "tools\setup\setup_assaultfire_hosts.ps1"; if(-not(Test-Path -LiteralPath $hostScript -PathType Leaf)){throw "Hosts setup helper is missing: $hostScript"}
    Write-Host "[SETUP] Repairing Assault Fire localhost mappings..."
    if(-not (Test-IsAdministrator)){throw "The launcher must be run as Administrator before repairing Windows hosts mappings."}
    & $currentPowerShellExe -NoProfile -ExecutionPolicy Bypass -File $hostScript
    if($LASTEXITCODE -ne 0){throw "Windows hosts setup helper failed with exit code $LASTEXITCODE."}
    ipconfig /flushdns | Out-Null; if(-not(Test-AFHosts)){throw "Windows hosts setup finished but the required 127.0.0.1 mappings did not verify."}; Write-Host "[OK] Hosts mappings verified." -ForegroundColor Green
}

function Stop-RunningGameProcesses {
    $running=@(Get-Process -Name "client","TGame","TGame_AFDEV" -ErrorAction SilentlyContinue)
    if($running.Count -gt 0){Write-Host ""; Write-Host "Assault Fire is already running. Setup/patching needs it closed."; foreach($p in $running){Write-Host ("  {0} PID={1}" -f $p.ProcessName,$p.Id)}; if(-not(Read-LauncherChoice -Name "CloseGameProcesses" -Prompt "Close these Assault Fire processes automatically?" -DefaultYes $true)){throw "Close client.exe/TGame.exe/TGame_AFDEV.exe, then run this script again."}; foreach($p in $running){Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue}; Start-Sleep -Milliseconds 800}
    $debugger=Get-Process -Name "x32dbg" -ErrorAction SilentlyContinue|Select-Object -First 1
    if($debugger){Write-Host ""; Write-Host "x32dbg is running, but the clean one-click launch helper requires it detached/closed."; if(-not(Read-LauncherChoice -Name "CloseDebugger" -Prompt "Close x32dbg automatically?" -DefaultYes $true)){throw "Close or detach x32dbg, then run this script again."}; Stop-Process -Id $debugger.Id -Force -ErrorAction SilentlyContinue; Start-Sleep -Milliseconds 500}
}

function Stop-ExistingEmulatorServer([string]$RepoRoot){$matches=@(); try{$matches=@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue|Where-Object{$_.CommandLine -and $_.CommandLine -match "assaultfire_server_v143b\.py" -and $_.CommandLine.IndexOf($RepoRoot,[System.StringComparison]::OrdinalIgnoreCase) -ge 0})}catch{}; if($matches.Count -eq 0){return}; Write-Host ""; foreach($p in $matches){Write-Host "[FOUND] Existing emulator server PID=$($p.ProcessId)"}; if(-not(Read-LauncherChoice -Name "StopExistingServer" -Prompt "Stop the existing emulator server and start a clean one?" -DefaultYes $true)){throw "An emulator server is already running."}; foreach($p in $matches){Stop-Process -Id ([int]$p.ProcessId) -Force -ErrorAction SilentlyContinue}; Start-Sleep -Milliseconds 800}

function Ensure-PermanentTCLS([string]$RepoRoot,[string]$GameRoot,[string]$VenvPython){
    $tcls=Join-Path $GameRoot "TCLS\Tenio\TCLS.dll"; $hash=Get-Sha256 $tcls
    if($hash -eq $TCLS_PATCHED_SHA256){Write-Host "[OK] TCLS.dll matches the verified patched PH build: $hash" -ForegroundColor Green; if(-not(Read-LauncherChoice -Name "ContinueWithPatchedTcls" -Prompt "TCLS.dll is already patched. Continue without patching it again?" -DefaultYes $true)){throw "You chose not to continue with the already-patched TCLS.dll. No patch was applied."}; return}
    if($hash -ne $TCLS_ORIGINAL_SHA256){Write-Host "[WARN] TCLS.dll hash is not the verified original or known patched hash." -ForegroundColor Yellow; Write-Host "       Expected original: $TCLS_ORIGINAL_SHA256"; Write-Host "       Expected patched:  $TCLS_PATCHED_SHA256"; Write-Host "       Found:             $hash"; $unknownTclsAnswer=Read-Host "Do you believe this TCLS.dll is already custom-patched? [y/N]"; if($unknownTclsAnswer -match "^(?i)y(es)?$"){throw "You confirmed a custom-patched TCLS.dll, but the server only accepts the exact verified patched hash. Nothing was modified."}; throw "Unsupported TCLS.dll hash. Use the verified original or exact verified patched PH v1.0.0.24 DLL. Nothing was modified."}
    Write-Host ""; Write-Host "Your TCLS.dll is the verified ORIGINAL PH build."; Write-Host "The emulator can install the verified permanent raw-PEM compatibility patch once."; Write-Host "It creates TCLS.dll.bak and verifies the final SHA256."; Write-Host ""
    if(-not(Read-LauncherChoice -Name "ApplyTclsPatch" -Prompt "Patch TCLS.dll permanently so you do not have to do this setup again?" -DefaultYes $true)){throw("The current emulator preflight requires the verified patched TCLS build. "+"No patch was applied because you selected No.")}
    $patcher=Join-Path $RepoRoot "tools\patches\patch_tcls_apclient_raw_pem.py"; Invoke-Checked -Exe $VenvPython -Arguments @($patcher,$tcls,"--apply") -Description "apply verified permanent TCLS compatibility patch"; $after=Get-Sha256 $tcls; if($after -ne $TCLS_PATCHED_SHA256){throw "TCLS.dll patch finished but final SHA256 is unexpected: $after"}; Write-Host "[OK] TCLS.dll permanently patched and verified." -ForegroundColor Green
}

function Ensure-Keys([string]$RepoRoot,[string]$GameRoot,[string]$VenvPython){
    $privateKey=Join-Path $RepoRoot "server\PRIVATE.PEM"; $publicKey=Join-Path $RepoRoot "generated\APClient.dat"; $clientConfig=Join-Path $GameRoot "TCLS\config"; $clientAP=Join-Path $clientConfig "APClient.dat"; $generator=Join-Path $RepoRoot "tools\setup\generate_local_rsa_keypair.py"; $diagnose=Join-Path $RepoRoot "tools\patches\diagnose_tcls_apclient.py"
    if(-not(Test-Path -LiteralPath $clientConfig -PathType Container)){throw "Missing client config folder: $clientConfig"}
    $useOwnPrivateKey=$false; $forceNewPair=$false
    if(Test-Path -LiteralPath $privateKey -PathType Leaf){$useOwnPrivateKey=Read-LauncherChoice -Name "UseExistingPrivateKey" -Prompt "Do you have your own PRIVATE.PEM here and want to keep using it?" -DefaultYes $true; $forceNewPair=-not $useOwnPrivateKey}
    else {if(Read-LauncherChoice -Name "UseOwnPrivateKey" -Prompt "Do you have your own PRIVATE.PEM key to use?" -DefaultYes $false -DoNotRemember){$ownPrivateSource=Read-Host "Enter the full path to your PRIVATE.PEM"; if(-not(Test-Path -LiteralPath $ownPrivateSource -PathType Leaf)){throw "Your PRIVATE.PEM was not found at: $ownPrivateSource"}; New-Item -ItemType Directory -Path (Split-Path -Parent $privateKey) -Force|Out-Null; Copy-Item -LiteralPath $ownPrivateSource -Destination $privateKey -Force; $useOwnPrivateKey=$true}}
    if($useOwnPrivateKey){Write-Host "[CHECK] Keeping your PRIVATE.PEM and verifying it against TCLS/config/APClient.dat..."; & $VenvPython $diagnose --client-root $GameRoot; $ownKeyCheckExitCode=$LASTEXITCODE; if($ownKeyCheckExitCode -ne 0){Write-Host "[WARN] Your PRIVATE.PEM does not match the installed APClient.dat or verified TCLS build." -ForegroundColor Yellow; if(-not(Read-LauncherChoice -Name "OverwriteMismatchedPrivateKey" -Prompt "Replace your PRIVATE.PEM and APClient.dat with a new matching pair?" -DefaultYes $false -DoNotRemember)){throw "Your PRIVATE.PEM was preserved. Install its matching APClient.dat or rerun and choose Y to replace the local key pair."}; Backup-IfExists $privateKey "oneclick_mismatch"; Backup-IfExists $publicKey "oneclick_mismatch"; Backup-IfExists $clientAP "oneclick_mismatch"; Invoke-Checked -Exe $VenvPython -Arguments @($generator,"--client-config-dir",$clientConfig,"--force") -Description "replace mismatched key with a new local RSA/APClient pair"; & $VenvPython $diagnose --client-root $GameRoot; if($LASTEXITCODE -ne 0){throw "The replacement RSA/APClient pair did not pass TCLS verification. Backups were kept."}; Write-Host "[OK] Replacement RSA/APClient pair passed verification. Original files are backed up." -ForegroundColor Green; Set-LauncherPreference "UseExistingPrivateKey" $true; return}; Write-Host "[OK] Your PRIVATE.PEM matches the client APClient.dat and verified TCLS build. It was kept unchanged." -ForegroundColor Green; Set-LauncherPreference "UseExistingPrivateKey" $true; return}
    $needGenerate=($forceNewPair -or -not(Test-Path -LiteralPath $privateKey -PathType Leaf) -or -not(Test-Path -LiteralPath $publicKey -PathType Leaf))
    if($needGenerate){Write-Host "[SETUP] Local RSA/APClient pair is incomplete; generating a fresh matching pair..."; Backup-IfExists $privateKey "oneclick_old"; Backup-IfExists $publicKey "oneclick_old"; Invoke-Checked -Exe $VenvPython -Arguments @($generator,"--client-config-dir",$clientConfig,"--force") -Description "generate and install local RSA/APClient pair"; Set-LauncherPreference "UseExistingPrivateKey" $true}
    else {$copyNeeded=$true; if(Test-Path -LiteralPath $clientAP -PathType Leaf){try{$copyNeeded=((Get-Sha256 $clientAP) -ne (Get-Sha256 $publicKey))}catch{$copyNeeded=$true}}; if($copyNeeded){Write-Host "[SETUP] Installing this emulator's matching APClient.dat..."; Backup-IfExists $clientAP "oneclick_old"; Copy-Item -LiteralPath $publicKey -Destination $clientAP -Force}else{Write-Host "[OK] APClient.dat already matches the emulator public key." -ForegroundColor Green}}
    Write-Host "[CHECK] Verifying TCLS + APClient + PRIVATE.PEM..."; & $VenvPython $diagnose --client-root $GameRoot; if($LASTEXITCODE -eq 0){Write-Host "[OK] TCLS/RSA/APClient verification passed." -ForegroundColor Green; return}
    Write-Host "[REPAIR] Existing RSA files are inconsistent. Rebuilding the local pair..."; Backup-IfExists $privateKey "oneclick_mismatch"; Backup-IfExists $publicKey "oneclick_mismatch"; Backup-IfExists $clientAP "oneclick_mismatch"; Invoke-Checked -Exe $VenvPython -Arguments @($generator,"--client-config-dir",$clientConfig,"--force") -Description "regenerate matching local RSA/APClient pair"; & $VenvPython $diagnose --client-root $GameRoot; if($LASTEXITCODE -ne 0){throw "TCLS/RSA/APClient verification still fails after automatic repair."}; Write-Host "[OK] TCLS/RSA/APClient repaired and verified." -ForegroundColor Green; Set-LauncherPreference "UseExistingPrivateKey" $true
}

function Get-TGameBinaryCheck([string]$RepoRoot,[string]$Path,[string]$VenvPython,[switch]$Apply){
    $checker=Join-Path $RepoRoot "tools\patches\tgame_binary.py"; if(-not(Test-Path -LiteralPath $checker -PathType Leaf)){throw "TGame binary checker is missing: $checker"}
    $nativePreference=Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue; if($null -ne $nativePreference){$savedNativePreference=$nativePreference.Value; $PSNativeCommandUseErrorActionPreference=$false}
    $arguments=@(); if($Apply){$arguments+=@("--apply")}; $arguments+=@("--json",$Path)
    try{$output=& $VenvPython $checker @arguments; $exitCode=$LASTEXITCODE}catch{throw "TGame binary inspection/patch could not run: $($_.Exception.Message)"}finally{if($null -ne $nativePreference){$PSNativeCommandUseErrorActionPreference=$savedNativePreference}}
    # The checker intentionally returns exit code 1 for a well-formed
    # {status:"unsupported"} result. Parse the machine-readable result first
    # so Ensure-AFDev can report the precise unsupported-signature message.
    try{$result=($output -join "`n")|ConvertFrom-Json}catch{throw "TGame binary checker returned invalid output (exit $exitCode): $($output -join ' ')"}
    $knownStatuses=@("unpatched-compatible","already-patched","unsupported"); if($Apply){$knownStatuses+=@("patched")}
    if($result.status -notin $knownStatuses){throw "TGame binary checker returned an unknown status: $($result.status)"}
    if($exitCode -ne 0 -and $result.status -ne "unsupported"){throw "TGame binary inspection failed with exit code $exitCode. $($output -join ' ')"}
    return $result
}

function Get-ServerMoveBinaryCheck([string]$RepoRoot,[string]$Path,[string]$VenvPython,[switch]$Apply){
    $checker=Join-Path $RepoRoot "tools\patches\tgame_servermove_v4.py"; if(-not(Test-Path -LiteralPath $checker -PathType Leaf)){throw "ServerMove v4 binary checker is missing: $checker"}
    $nativePreference=Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue; if($null -ne $nativePreference){$savedNativePreference=$nativePreference.Value; $PSNativeCommandUseErrorActionPreference=$false}
    $arguments=@(); if($Apply){$arguments+=@("--apply")}; $arguments+=@("--json",$Path)
    try{$output=& $VenvPython $checker @arguments; $exitCode=$LASTEXITCODE}catch{throw "ServerMove v4 inspection/patch could not run: $($_.Exception.Message)"}finally{if($null -ne $nativePreference){$PSNativeCommandUseErrorActionPreference=$savedNativePreference}}
    try{$result=($output -join "`n")|ConvertFrom-Json}catch{throw "ServerMove v4 checker returned invalid output (exit $exitCode): $($output -join ' ')"}
    $knownStatuses=@("unpatched-compatible","already-patched","unsupported"); if($Apply){$knownStatuses+=@("patched")}
    if($result.status -notin $knownStatuses){throw "ServerMove v4 checker returned an unknown status: $($result.status)"}
    if($exitCode -ne 0 -and $result.status -ne "unsupported"){throw "ServerMove v4 inspection failed with exit code $exitCode. $($output -join ' ')"}
    return $result
}

function Ensure-AFDev([string]$GameRoot,[string]$VenvPython,[string]$RepoRoot){
    $win32=Join-Path $GameRoot "Binaries\Win32"; $tgame=Join-Path $win32 "TGame.exe"; $afdev=Join-Path $win32 "TGame_AFDEV.exe"; $tgameHash=Get-Sha256 $tgame
    if($tgameHash -eq $EXPECTED_TGAME_SHA256){Write-Host "[OK] TGame.exe matches the validated PH v1.0.0.24 build." -ForegroundColor Green}
    else{Write-Host "[CHECK] TGame.exe hash differs from the stock PH v1.0.0.24 build; verifying its datetime patch signature." -ForegroundColor Yellow; Write-Host "       Expected SHA256: $EXPECTED_TGAME_SHA256"; Write-Host "       Found SHA256:    $tgameHash"}

    $binaryCheck=Get-TGameBinaryCheck $RepoRoot $tgame $VenvPython
    if($binaryCheck.status -eq "unsupported"){
        throw("Unsupported TGame.exe. Its patch-site signature is not recognized: $($binaryCheck.message) Nothing was changed.")
    }
    if($binaryCheck.status -eq "unpatched-compatible"){
        Write-Host "[PATCH] Applying the verified datetime patch to TGame.exe. A byte-exact TGame.exe.bak is required first." -ForegroundColor Yellow
        $patchResult=Get-TGameBinaryCheck $RepoRoot $tgame $VenvPython -Apply
        if($patchResult.status -notin @("patched","already-patched")){
            throw "Permanent TGame datetime patch failed safely: $($patchResult.message)"
        }
        $backupPath="$tgame.bak"
        if(-not(Test-Path -LiteralPath $backupPath -PathType Leaf)){
            throw "The datetime patch completed without a TGame.exe.bak backup. TGame.exe was not accepted."
        }
        if((Get-Sha256 $backupPath) -ne $tgameHash){
            throw "TGame.exe.bak does not match the original TGame.exe. The patched game was not accepted."
        }
        $binaryCheck=Get-TGameBinaryCheck $RepoRoot $tgame $VenvPython
        if($binaryCheck.status -ne "already-patched"){
            throw "TGame.exe failed verification after the permanent datetime patch: $($binaryCheck.message)"
        }
        Write-Host "[OK] TGame.exe datetime patch is installed and verified; original saved as TGame.exe.bak." -ForegroundColor Green
    }
    else{
        Write-Host "[OK] TGame.exe already contains the fully verified datetime patch." -ForegroundColor Green
    }

    $replace=$false
    if(-not(Test-Path -LiteralPath $afdev -PathType Leaf)){
        $replace=$true
        Write-Host "[SETUP] TGame_AFDEV.exe is missing."
    }
    else{
        $afdevDate=Get-TGameBinaryCheck $RepoRoot $afdev $VenvPython
        $afdevMove=Get-ServerMoveBinaryCheck $RepoRoot $afdev $VenvPython
        if($afdevDate.status -ne "already-patched" -or $afdevMove.status -eq "unsupported"){
            Write-Host "[REPAIR] Existing TGame_AFDEV.exe is not the accepted datetime + ServerMove-v4 runtime." -ForegroundColor Yellow
            Backup-IfExists $afdev "oneclick_wrong_build"
            Backup-IfExists "$afdev.servermove-v4.bak" "oneclick_old_servermove_backup"
            $replace=$true
        }
        elseif($afdevMove.status -eq "already-patched"){
            Write-Host "[OK] TGame_AFDEV.exe already contains the verified ServerMove-v4 patch." -ForegroundColor Green
        }
    }

    if($replace){
        Write-Host "[SETUP] Creating TGame_AFDEV.exe from YOUR OWN verified TGame.exe..."
        Copy-Item -LiteralPath $tgame -Destination $afdev -Force
        $copiedDate=Get-TGameBinaryCheck $RepoRoot $afdev $VenvPython
        if($copiedDate.status -ne "already-patched"){
            throw "Fresh TGame_AFDEV.exe did not preserve the verified datetime patch."
        }
    }

    $moveCheck=Get-ServerMoveBinaryCheck $RepoRoot $afdev $VenvPython
    if($moveCheck.status -eq "unpatched-compatible"){
        Write-Host "[PATCH] Applying verified native ServerMove v4 to TGame_AFDEV.exe..." -ForegroundColor Yellow
        $moveResult=Get-ServerMoveBinaryCheck $RepoRoot $afdev $VenvPython -Apply
        if($moveResult.status -notin @("patched","already-patched")){
            throw "ServerMove v4 patch failed safely: $($moveResult.message)"
        }
    }
    elseif($moveCheck.status -eq "unsupported"){
        throw "TGame_AFDEV.exe failed ServerMove-v4 structural validation: $($moveCheck.message)"
    }

    $finalDate=Get-TGameBinaryCheck $RepoRoot $afdev $VenvPython
    $finalMove=Get-ServerMoveBinaryCheck $RepoRoot $afdev $VenvPython
    if($finalDate.status -ne "already-patched"){
        throw "TGame_AFDEV.exe failed final datetime verification."
    }
    if($finalMove.status -ne "already-patched"){
        throw "TGame_AFDEV.exe failed final ServerMove-v4 verification: $($finalMove.message)"
    }

    Write-Host "[OK] AFDEV runtime contains verified datetime + native ServerMove-v4 patches." -ForegroundColor Green
    Write-Host "     (Local private copy only; the emulator repository does not redistribute this game binary.)"
}

function Wait-ForLaunchGate([string]$StatusPath,[int]$TimeoutSeconds=45){$deadline=(Get-Date).AddSeconds($TimeoutSeconds); $last=$null; while((Get-Date)-lt $deadline){if(Test-Path -LiteralPath $StatusPath -PathType Leaf){try{$last=Get-Content -LiteralPath $StatusPath -Raw|ConvertFrom-Json; if($last.launch_ready -eq $true){return $last}; if($last.passed -eq $false -and $last.errors -and @($last.errors).Count -gt 0){$joined=(@($last.errors)-join "; "); throw "Server preflight failed: $joined"}}catch{if($_.Exception.Message -like "Server preflight failed:*"){throw}}}; Start-Sleep -Milliseconds 250}; if($last){$errors=if($last.errors){@($last.errors)-join "; "}else{"no detailed error was recorded"}; throw "Timed out waiting for game launch gate UNLOCKED. Last preflight: $errors"}; throw "Timed out waiting for server preflight_status.json."}

function Wait-ForTclsHelperArmed(
    [string]$LogPath,
    [System.Diagnostics.Process]$HelperProcess,
    [int]$TimeoutSeconds = 120
) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $logText = ""
        if (Test-Path -LiteralPath $LogPath -PathType Leaf) {
            $logText = Get-Content -LiteralPath $LogPath -Raw -ErrorAction SilentlyContinue
        }

        if ($logText -match '(?im)^\s*(?:\[SAFE STOP\]|ERROR:)') {
            throw "The launch helper reported an error before arming TCLS. See helper log: $LogPath"
        }
        if ($logText -match '(?m)^\s*TCLS ARMED\s*$') {
            return
        }
        if ($HelperProcess.HasExited) {
            $tail = @()
            if (Test-Path -LiteralPath $LogPath -PathType Leaf) {
                $tail = @(Get-Content -LiteralPath $LogPath -Tail 12 -ErrorAction SilentlyContinue)
            }
            $detail = if ($tail.Count -gt 0) { $tail -join [Environment]::NewLine } else { "The helper log is empty or was never written." }
            throw "The launch helper exited with code $($HelperProcess.ExitCode) before reporting TCLS ARMED. $detail Helper log: $LogPath"
        }

        Start-Sleep -Milliseconds 250
    }

    $tail = @()
    if (Test-Path -LiteralPath $LogPath -PathType Leaf) {
        $tail = @(Get-Content -LiteralPath $LogPath -Tail 12 -ErrorAction SilentlyContinue)
    }
    $detail = if ($tail.Count -gt 0) { $tail -join [Environment]::NewLine } else { "No helper output was written." }
    throw "Timed out after $TimeoutSeconds seconds waiting for the launch helper to report TCLS ARMED. $detail Helper log: $LogPath"
}

function Wait-ForTgameHelperComplete(
    [string]$LogPath,
    [System.Diagnostics.Process]$HelperProcess,
    [int]$TimeoutSeconds = 60
) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $logText = ""
        if (Test-Path -LiteralPath $LogPath -PathType Leaf) {
            $logText = Get-Content -LiteralPath $LogPath -Raw -ErrorAction SilentlyContinue
        }

        if ($logText -match '(?im)^\s*(?:\[SAFE STOP\]|ERROR:)') {
            $tail = @(Get-Content -LiteralPath $LogPath -Tail 16 -ErrorAction SilentlyContinue)
            throw "The launch helper failed before resuming TGame. $($tail -join [Environment]::NewLine) Helper log: $LogPath"
        }
        if ($logText -match '(?m)^\s*\[AF-TGAME-RESUMED\]\s+PID=\d+\s*$') {
            return
        }
        if ($HelperProcess.HasExited) {
            $tail = @(Get-Content -LiteralPath $LogPath -Tail 16 -ErrorAction SilentlyContinue)
            $detail = if ($tail.Count -gt 0) { $tail -join [Environment]::NewLine } else { "The helper log is empty or was never written." }
            throw "The launch helper exited with code $($HelperProcess.ExitCode) before confirming the TGame patch. $detail Helper log: $LogPath"
        }

        Start-Sleep -Milliseconds 250
    }

    $tail = @()
    if (Test-Path -LiteralPath $LogPath -PathType Leaf) {
        $tail = @(Get-Content -LiteralPath $LogPath -Tail 16 -ErrorAction SilentlyContinue)
    }
    $detail = if ($tail.Count -gt 0) { $tail -join [Environment]::NewLine } else { "No helper output was written." }
    throw "Timed out after $TimeoutSeconds seconds waiting for the datetime patch and TGame resume. $detail Helper log: $LogPath"
}

Initialize-LauncherConfig
Write-Title "Assault Fire PH - ONE CLICK SETUP + PLAY"
Write-Host "[AF-ONECLICK] Launcher revision: $LAUNCHER_REVISION"
$self=$MyInvocation.MyCommand.Path; if(-not $self){Stop-WithMessage "Could not determine the launcher script path."}; $self=(Resolve-Path -LiteralPath $self).Path
$consoleHelper=Join-Path $PSScriptRoot "tools\setup\af_console_nonblocking.ps1"; if(Test-Path -LiteralPath $consoleHelper -PathType Leaf){. $consoleHelper; Disable-AFConsoleBlockingSelection; Write-Host "[AF-ONECLICK] Console mouse selection: NON-BLOCKING"}else{Write-Host "[AF-ONECLICK] WARNING: console non-blocking helper is missing." -ForegroundColor Yellow}
try{
    $repoRoot=Split-Path -Parent $self; if(-not(Test-Path -LiteralPath (Join-Path $repoRoot "server\assaultfire_server_v143b.py") -PathType Leaf)){throw "START_ASSAULT_FIRE.ps1 must stay in the root of the af-emulator folder."}
    $gameRoot=Find-GameRoot $repoRoot; if(-not $gameRoot){throw("Could not find the Assault Fire PH game root. Put the ENTIRE af-emulator folder inside your "+"Assault Fire game folder, or copy the emulator contents directly into the game root. "+"The game root must contain TCLS\client.exe, TCLS\Tenio\TCLS.dll, and Binaries\Win32\TGame.exe.")}
    $win32=Join-Path $gameRoot "Binaries\Win32"; $clientExe=Join-Path $gameRoot "TCLS\client.exe"; Write-Host "[OK] Emulator : $repoRoot" -ForegroundColor Green; Write-Host "[OK] Game root: $gameRoot" -ForegroundColor Green
    Write-Step "Closing old Assault Fire processes"; Stop-RunningGameProcesses; Stop-ExistingEmulatorServer $repoRoot
    Write-Step "Checking Python and emulator dependencies"; $bootstrapPython=Ensure-SupportedPython $repoRoot $gameRoot; $venvPython=Ensure-Venv $repoRoot $gameRoot $bootstrapPython
    Write-Step "Checking the supported TGame build and patch signature"; Ensure-AFDev $gameRoot $venvPython $repoRoot
    Write-Step "Checking TCLS.dll"; Ensure-PermanentTCLS $repoRoot $gameRoot $venvPython
    Write-Step "Preparing the local RSA/APClient pair"; Ensure-Keys $repoRoot $gameRoot $venvPython
    Write-Step "Checking Windows hosts mappings"; Ensure-Hosts $repoRoot
    $env:AF_CLIENT_ROOT=$gameRoot; $env:AF_GAME_DIR=$win32; $env:AF_DS_SPAWNER_ENABLED="1"
    Write-Host ""; Write-Host "[READY] First-time setup checks are complete." -ForegroundColor Green; Write-Host "[READY] PvE DS spawning is enabled."; Write-Host "[READY] This launcher and its runtime components are running as Administrator."; Write-Host "[READY] You no longer need to set AF_CLIENT_ROOT / AF_GAME_DIR manually."
    if($SetupOnly){Write-Host ""; Write-Host "-SetupOnly was selected, so the server/client will not be launched."; Read-Host "Press Enter to close"; exit 0}
    Write-Step "Starting the emulator server"; $statusPath=Join-Path $repoRoot "runtime\preflight_status.json"; Remove-Item -LiteralPath $statusPath -Force -ErrorAction SilentlyContinue
    $serverScript=Join-Path $repoRoot "server\assaultfire_server_v143b.py"; $serverCommand=(". "+(Quote-PS $consoleHelper)+"; "+"Disable-AFConsoleBlockingSelection; "+'$env:AF_CLIENT_ROOT='+(Quote-PS $gameRoot)+"; "+'$env:AF_GAME_DIR='+(Quote-PS $win32)+"; "+'$env:AF_DS_SPAWNER_ENABLED='+(Quote-PS "1")+"; "+'$env:AF_DS_PYTHON='+(Quote-PS $venvPython)+"; "+'$env:AF_RUNTIME_MODE='+(Quote-PS "development")+"; "+'$env:AF_DEV_WEB='+(Quote-PS "1")+"; "+'$env:AF_WEB_OPEN_BROWSER='+(Quote-PS "1")+"; "+"Set-Location -LiteralPath "+(Quote-PS $repoRoot)+"; "+"Write-Host '[AF-ADMIN] Emulator server running elevated.' -ForegroundColor Green; "+"& "+(Quote-PS $venvPython)+" "+(Quote-PS $serverScript))
    Write-Host "[ADMIN] The emulator server inherits this launcher's Administrator token." -ForegroundColor Green
    try{$serverWindow=Start-Process -FilePath $currentPowerShellExe -WorkingDirectory $repoRoot -PassThru -ArgumentList @("-NoProfile","-NoExit","-ExecutionPolicy","Bypass","-Command",$serverCommand)}catch{throw "Could not start the elevated emulator server: $($_.Exception.Message)"}
    Write-Host "[WAIT] Waiting for server preflight and listener gate..."; $status=Wait-ForLaunchGate $statusPath 45; Write-Host "[OK] Server launch gate is UNLOCKED. Server PID=$($status.server_pid)" -ForegroundColor Green
    Write-Host "[ACCOUNT] First-time players must register on the local account page before signing in." -ForegroundColor Yellow
    Write-Host "[ACCOUNT] The page should open automatically. If not, copy the [WEB] Registration URL from the server window (usually http://127.0.0.1:8080/register)."
    Write-Host "[ACCOUNT] Use the same username and password in the Assault Fire launcher. Register only once for this server database."
    Write-Step "Starting the automatic TGame launch helper"
    $helper = Join-Path $repoRoot "tools\patches\patch_tcls_suspended_launch.py"
    $helperLog = Join-Path $gameRoot "af_tgame_launch_helper.log"
    if (Test-Path -LiteralPath $helperLog) {
        Remove-Item -LiteralPath $helperLog -Force -ErrorAction Stop
    }
    New-Item -ItemType File -Path $helperLog -Force | Out-Null
    # Windows PowerShell 5.1's Tee-Object writes UTF-16LE (Unicode), while
    # PowerShell 7 writes UTF-8 without a BOM. Keep the helper's boot messages
    # in the same encoding so the log stays readable while it is being tailed.
    $helperLogEncoding = if ($PSVersionTable.PSEdition -eq "Core") { "utf8" } else { "Unicode" }
    $helperCommand = (
        '$ErrorActionPreference = "Continue"; ' +
        'Add-Content -LiteralPath ' + (Quote-PS $helperLog) + ' -Value "[AF-HELPER-BOOT] PowerShell helper command entered." -Encoding ' + $helperLogEncoding + '; ' +
        'try { ' +
        ". " + (Quote-PS $consoleHelper) + "; " +
        "Disable-AFConsoleBlockingSelection; " +
        '$env:AF_CLIENT_ROOT=' + (Quote-PS $gameRoot) + "; " +
        "Set-Location -LiteralPath " + (Quote-PS $repoRoot) + "; " +
        "Write-Host '[AF-ADMIN] TGame launch/OpenProcess helper running elevated.' -ForegroundColor Green; " +
        'Add-Content -LiteralPath ' + (Quote-PS $helperLog) + ' -Value "[AF-HELPER-BOOT] Starting Python helper." -Encoding ' + $helperLogEncoding + '; ' +
        "& " + (Quote-PS $venvPython) + " -u " + (Quote-PS $helper) +
        " --timeout 900 2>&1 | Tee-Object -FilePath " + (Quote-PS $helperLog) + " -Append; " +
        '$helperExitCode = $LASTEXITCODE; ' +
        'Add-Content -LiteralPath ' + (Quote-PS $helperLog) + ' -Value ("[AF-HELPER-BOOT] Python helper exited with code " + $helperExitCode) -Encoding ' + $helperLogEncoding + '; ' +
        'exit $helperExitCode; ' +
        '} catch { ' +
        '$helperFailure = "[AF-HELPER-BOOT] PowerShell helper failed: " + $_.Exception.Message; ' +
        'Add-Content -LiteralPath ' + (Quote-PS $helperLog) + ' -Value $helperFailure -Encoding ' + $helperLogEncoding + '; ' +
        'Write-Error $_; exit 1 }'
    )
    $helperEncodedCommand = ConvertTo-PowerShellEncodedCommand $helperCommand
    Write-Host "[ADMIN] The TGame launch helper inherits this launcher's Administrator token." -ForegroundColor Green
    try {
        $helperWindow = Start-Process -FilePath $currentPowerShellExe -WorkingDirectory $repoRoot -PassThru -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $helperEncodedCommand
        )
    } catch {
        throw "Could not start the elevated TGame launch helper: $($_.Exception.Message)"
    }

    Write-Step "Launching Assault Fire client.exe for you"
    Start-Process -FilePath $clientExe -WorkingDirectory (Split-Path -Parent $clientExe) | Out-Null
    Write-Host "[WAIT] Log in to the Assault Fire launcher now, but do not click START yet." -ForegroundColor Yellow
    Write-Host "[WAIT] Waiting for the elevated helper to arm TCLS..."
    Wait-ForTclsHelperArmed $helperLog $helperWindow 120
    Write-Host "[OK] TCLS is armed. You can click START now." -ForegroundColor Green

    Write-Title "YOU ARE DONE WITH SETUP"
    Write-Host "The emulator server is running."
    Write-Host "The launch helper is armed and waiting for the game launch."
    Write-Host "The Assault Fire launcher was opened automatically."
    Write-Host ""
    Write-Host "What you do now:" -ForegroundColor Green
    Write-Host "  1. First time only: register on the local account page opened in your browser."
    Write-Host "  2. Log in in the Assault Fire launcher with that same username and password."
    Write-Host "  3. When the START button appears, click START."
    Write-Host ""
    Write-Host "You do NOT need to run the server, patcher, hosts helper, or client.exe manually anymore."
    Write-Host "The TGame datetime patch is permanent and backed up as TGame.exe.bak; launch suspension is temporary."
    Write-Host ""
    Write-Host "[WAIT] Waiting for TGame.exe to appear (up to 15 minutes)..."
    $deadline = (Get-Date).AddMinutes(15)
    while ((Get-Date) -lt $deadline) {
        $game = Get-Process -Name "TGame" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($game) {
            Write-Host ""
            Write-Host "[WAIT] TGame.exe appeared as PID=$($game.Id). Waiting for the datetime patch and resume..."
            Wait-ForTgameHelperComplete $helperLog $helperWindow 60
            Write-Host "[SUCCESS] TGame datetime patch was verified and its primary thread resumed." -ForegroundColor Green
            Write-Host "[SUCCESS] TGame.exe launched. PID=$($game.Id)" -ForegroundColor Green
            if ($KeepServer) {
                Write-Host "[SUCCESS] -KeepServer was selected; the emulator server will remain running."
                Start-Sleep -Seconds 2
                exit 0
            }
            Write-Host "[SESSION] This one-click window will stay open while you play."
            Write-Host "[SESSION] When TGame.exe closes, it will stop the emulator server automatically."
            try { Wait-Process -Id $game.Id } catch {}
            Write-Host ""
            Write-Host "[CLEANUP] TGame.exe closed. Stopping the emulator server..."
            try { if ($status.server_pid) { Stop-Process -Id ([int]$status.server_pid) -Force -ErrorAction SilentlyContinue } } catch {}
            try { if ($serverWindow -and -not $serverWindow.HasExited) { Stop-Process -Id $serverWindow.Id -Force -ErrorAction SilentlyContinue } } catch {}
            Write-Host "[CLEANUP] Done." -ForegroundColor Green
            Read-Host "Press Enter to close"
            exit 0
        }

        if ($helperWindow.HasExited -and $helperWindow.ExitCode -ne 0) {
            throw "The automatic launch helper exited with code $($helperWindow.ExitCode). See helper log: $helperLog"
        }
        Start-Sleep -Seconds 1
    }
    throw "Timed out waiting for TGame.exe. The server is still running; check the launcher/helper window for the exact error."
}catch{Stop-WithMessage $_.Exception.Message}
