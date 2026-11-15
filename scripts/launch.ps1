<#
  Smart Irrigation launcher (Windows). Usually started through launch.bat.

    launch.bat                     interactive menu
    launch.bat dashboard           web dashboard only; start sensors/Contiki-NG/Cooja from the browser
    launch.bat python [scenario]   Python sensor simulator + controller + dashboard
    launch.bat native [scenario]   Contiki-NG node (native target, in WSL) + controller + dashboard
    launch.bat cooja  [scenario]   Cooja GUI with the Contiki-NG node + controller + dashboard
    launch.bat stop                stop everything started by this launcher
    launch.bat status              show what is running
    launch.bat setup               one-time install: Python venv + Contiki-NG/Cooja in WSL + desktop shortcuts
    launch.bat shortcut            (re)create the desktop shortcuts
    launch.bat test                run the pytest suite

  scenario: 0 normal, 1 invalid, 2 negative, 3 missing, 4 delayed,
            5 intermittent, 6 malformed, 7 mixed (default). Names work too.
#>
param(
    [Parameter(Position = 0)][string]$Mode = "",
    [Parameter(Position = 1)][string]$Scenario = "mixed",
    [switch]$NoBrowser
)

# "Continue": in Windows PowerShell 5.1, stderr from native tools (taskkill, wsl) would
# otherwise abort the script. Real failures are raised explicitly with `throw`.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Runtime = Join-Path $Root "runtime"
$PidFile = Join-Path $Runtime "launcher_pids.txt"
$DashboardUrl = "http://localhost:8501"
$Scenarios = @("normal", "invalid", "negative", "missing", "delayed", "intermittent", "malformed", "mixed")

function Say($msg, $color = "Green") { Write-Host "==> $msg" -ForegroundColor $color }

function Get-Python {
    foreach ($p in @(".venv-windows\Scripts\python.exe", ".venv\Scripts\python.exe")) {
        $full = Join-Path $Root $p
        if (Test-Path $full) { return $full }
    }
    return $null
}

function Resolve-Scenario([string]$s) {
    if ($s -match '^[0-7]$') { return [int]$s }
    $i = [array]::IndexOf($Scenarios, $s.ToLower())
    if ($i -lt 0) { throw "Unknown scenario '$s'. Use 0-7 or one of: $($Scenarios -join ', ')" }
    return $i
}

function Test-Wsl {
    $null = & wsl.exe --status 2>$null
    if ($LASTEXITCODE -ne 0) { throw "WSL is not available. Install it with: wsl --install -d Ubuntu" }
}

function Invoke-Wsl([string]$bashArgs) {
    # Runs in the project folder (WSL maps the current Windows directory to /mnt/c/...).
    # Out-Host: show the output but keep it out of the return value (which is the exit code only).
    & wsl.exe -- bash $bashArgs.Split(" ") | Out-Host
    return [int]$LASTEXITCODE
}

# Opens a titled console window running $command from the project root; remembers its PID.
function Start-Window([string]$title, [string]$command, [string]$style = "Normal") {
    New-Item -ItemType Directory -Force $Runtime | Out-Null
    $line = "title $title && cd /d `"$Root`" && $command"
    $p = Start-Process cmd.exe -ArgumentList "/k", $line -PassThru -WindowStyle $style
    Add-Content -Path $PidFile -Value "$($p.Id) $title"
    Say "started: $title"
}

function Stop-All {
    Say "Stopping running components" "Yellow"
    if (Test-Path $PidFile) {
        foreach ($entry in Get-Content $PidFile) {
            $id = ($entry -split ' ')[0]
            & taskkill.exe /PID $id /T /F 2>$null | Out-Null
        }
        Remove-Item $PidFile -Force
    }
    # Anything from this project started some other way (e.g. by hand).
    $patterns = @("*controller.main*", "*simulator.sensor_simulator*", "*simulator.contiki_bridge*", "*streamlit*dashboard*app.py*")
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='streamlit.exe'" | Where-Object {
        $cl = $_.CommandLine; $cl -and ($patterns | Where-Object { $cl -like $_ })
    } | ForEach-Object { & taskkill.exe /PID $_.ProcessId /T /F 2>$null | Out-Null }
    # Contiki-NG / Cooja inside WSL (only if WSL is already running, so we don't boot it).
    $running = (& wsl.exe --list --running --quiet 2>$null) -join ""
    if ($running -replace "`0", "" -match '\S') {
        & wsl.exe -- bash -c "pkill -f sensor_node.native; pkill -f 'org.contikios.cooja'; pkill -f 'GradleWrapperMain.*run'" 2>$null
    }
    Say "Stopped. (The pump state resets to OFF when the controller starts again.)" "Yellow"
}

function Show-Status {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='streamlit.exe'" |
        Where-Object { $_.CommandLine -match 'controller\.main|sensor_simulator|contiki_bridge|streamlit' }
    # A venv python.exe is a stub that spawns the real interpreter: list only the outer process.
    $ids = @($procs | ForEach-Object { $_.ProcessId })
    $procs = $procs | Where-Object { $ids -notcontains $_.ParentProcessId }
    if ($procs) {
        $procs | ForEach-Object {
            $what = if ($_.CommandLine -match 'controller\.main') { "controller" }
                    elseif ($_.CommandLine -match 'sensor_simulator') { "simulator" }
                    elseif ($_.CommandLine -match 'contiki_bridge') { "contiki-ng" } else { "dashboard" }
            "{0,-11} pid {1}" -f $what, $_.ProcessId
        } | Sort-Object -Unique
    } else { "nothing from this project is running" }
    $state = Join-Path $Runtime "controller_state.json"
    if (Test-Path $state) {
        $s = Get-Content $state -Raw | ConvertFrom-Json
        "last state: moisture=$($s.soil_moisture)%  sensor=$($s.sensor_status)  pump=$($s.pump_state)  ($($s.updated_at))"
    }
}

function Wait-Dashboard {
    if ($NoBrowser) { return }
    Say "Waiting for the dashboard..."
    for ($i = 0; $i -lt 40; $i++) {
        try {
            $null = Invoke-WebRequest $DashboardUrl -UseBasicParsing -TimeoutSec 2
            Start-Process $DashboardUrl
            Say "Dashboard open: $DashboardUrl"
            return
        } catch { Start-Sleep -Milliseconds 750 }
    }
    Say "Dashboard not answering yet; open $DashboardUrl manually." "Yellow"
}

function Test-Dashboard {
    try { $null = Invoke-WebRequest $DashboardUrl -UseBasicParsing -TimeoutSec 2; return $true } catch { return $false }
}

function Start-Dashboard($py) {
    Start-Window "Irrigation - Dashboard" "`"$py`" -m streamlit run dashboard/app.py --server.headless true --server.port 8501" "Minimized"
}

# Every controller listens on UDP :5005 (Python simulator / Contiki-NG bridge) AND on
# Cooja's Serial Socket :60001, so the dashboard can switch sensor sources freely.
function Start-Controller($py) {
    Start-Window "Irrigation - Controller" "`"$py`" -m controller.main --source udp --source serial"
    Start-Sleep -Seconds 1
}

function Start-DashboardMode {
    $py = Require-Python
    if (Test-Dashboard) {
        Say "Dashboard already running"
    } else {
        Say "Starting the web dashboard (its console window is minimized)"
        Start-Dashboard $py
    }
    Wait-Dashboard
    Write-Host ""
    Write-Host "In the dashboard sidebar pick a sensor source - Python simulator, Contiki-NG node" -ForegroundColor Cyan
    Write-Host "or Cooja GUI - and press Start; the controller starts automatically." -ForegroundColor Cyan
}

function New-Shortcuts {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shell = New-Object -ComObject WScript.Shell
    $items = @(
        @{ Name = "Smart Irrigation Dashboard"; Args = "dashboard"; Icon = "$env:SystemRoot\System32\imageres.dll,1023"; Desc = "Open the irrigation web dashboard" },
        @{ Name = "Smart Irrigation Launcher"; Args = ""; Icon = "$env:SystemRoot\System32\imageres.dll,76"; Desc = "Launcher menu (simulator / Contiki-NG / Cooja)" }
    )
    foreach ($it in $items) {
        $lnk = $shell.CreateShortcut((Join-Path $desktop "$($it.Name).lnk"))
        $lnk.TargetPath = Join-Path $Root "launch.bat"
        $lnk.Arguments = $it.Args
        $lnk.WorkingDirectory = $Root
        $lnk.IconLocation = $it.Icon
        $lnk.Description = $it.Desc
        if ($it.Args) { $lnk.WindowStyle = 7 }  # minimized: the dashboard shortcut needs no console
        $lnk.Save()
        Say "Desktop shortcut: $($it.Name)"
    }
}

function Require-Python {
    $py = Get-Python
    if (-not $py) { throw "No Python venv found. Run: launch.bat setup" }
    return $py
}

function Start-PythonMode([int]$n) {
    $py = Require-Python
    Stop-All
    Say "Mode: Python simulator, scenario $n ($($Scenarios[$n]))"
    Start-Controller $py
    Start-Window "Irrigation - Sensor simulator" "`"$py`" -m simulator.sensor_simulator --scenario $($Scenarios[$n]) --interval 2"
    Start-Dashboard $py
    Wait-Dashboard
}

function Start-NativeMode([int]$n) {
    $py = Require-Python
    Test-Wsl
    Say "Mode: Contiki-NG native node in WSL, FAULT_SCENARIO=$n ($($Scenarios[$n]))"
    if ((Invoke-Wsl "scripts/contiki.sh build-native $n") -ne 0) {
        throw "Contiki-NG build failed. If this is the first run, do: launch.bat setup"
    }
    Stop-All
    Start-Controller $py
    # The bridge runs the node in WSL, shows its serial console and forwards readings over UDP.
    Start-Window "Irrigation - Contiki-NG node" "`"$py`" -m simulator.contiki_bridge --scenario $n"
    Start-Dashboard $py
    Wait-Dashboard
}

function Start-CoojaMode([int]$n) {
    $py = Require-Python
    Test-Wsl
    Say "Mode: Cooja simulation, FAULT_SCENARIO=$n ($($Scenarios[$n]))"
    if ((Invoke-Wsl "scripts/contiki.sh check") -ne 0) {
        throw "Contiki-NG/Cooja not fully installed. Run: launch.bat setup"
    }
    Stop-All
    Start-Window "Irrigation - Cooja" "wsl.exe -- bash scripts/contiki.sh cooja $n"
    # The controller keeps retrying until Cooja's Serial Socket (port 60001) is up.
    Start-Controller $py
    Start-Dashboard $py
    Write-Host ""
    Write-Host "Cooja compiles the node and opens in its own window (first start: 1-3 min)." -ForegroundColor Cyan
    Write-Host "The simulation starts automatically at real-time speed; readings arrive every 5 s." -ForegroundColor Cyan
    Wait-Dashboard
}

function Invoke-Setup {
    if (-not (Get-Python)) {
        Say "Creating Python venv (.venv-windows)"
        & python -m venv .venv-windows
        & .venv-windows\Scripts\python.exe -m pip install --upgrade pip
        & .venv-windows\Scripts\python.exe -m pip install -r requirements.txt
    } else { Say "Python venv already present" }
    New-Shortcuts
    Test-Wsl
    Say "Installing Contiki-NG + Cooja in WSL (asks for your Ubuntu sudo password)"
    $null = Invoke-Wsl "scripts/contiki.sh setup"
}

function Show-Menu {
    Write-Host ""
    Write-Host "  Fault-Aware Smart Irrigation - launcher" -ForegroundColor Cyan
    Write-Host "  ---------------------------------------"
    Write-Host "  1  Web dashboard      (control everything from the browser)  [Enter]"
    Write-Host "  2  Python simulator   (quickest, Windows only)"
    Write-Host "  3  Contiki-NG node    (native target in WSL, live console window)"
    Write-Host "  4  Cooja simulation   (Contiki-NG node in the Cooja GUI)"
    Write-Host "  5  Stop everything"
    Write-Host "  6  Status"
    Write-Host "  7  Setup (one-time: Python venv + Contiki-NG/Cooja in WSL + desktop shortcuts)"
    Write-Host "  8  Run tests"
    Write-Host "  Q  Quit"
    $choice = Read-Host "`n  Choose"
    if (-not $choice) { $choice = "1" }
    $map = @{ "1" = "dashboard"; "2" = "python"; "3" = "native"; "4" = "cooja"; "5" = "stop"; "6" = "status"; "7" = "setup"; "8" = "test" }
    if (-not $map.ContainsKey($choice)) { exit 0 }
    $m = $map[$choice]
    if ($m -in @("python", "native", "cooja")) {
        Write-Host ""
        for ($i = 0; $i -lt 8; $i++) { Write-Host ("    {0}  {1}" -f $i, $Scenarios[$i]) }
        $s = Read-Host "  Fault scenario [7 = mixed]"
        if ($s) { $script:Scenario = $s }
    }
    return $m
}

try {
    if (-not $Mode) { $Mode = Show-Menu }
    switch ($Mode.ToLower()) {
        "dashboard" { Start-DashboardMode }
        "shortcut"  { New-Shortcuts }
        "python" { Start-PythonMode (Resolve-Scenario $Scenario) }
        "native" { Start-NativeMode (Resolve-Scenario $Scenario) }
        "cooja"  { Start-CoojaMode  (Resolve-Scenario $Scenario) }
        "stop"   { Stop-All }
        "status" { Show-Status }
        "setup"  { Invoke-Setup }
        "test"   { & (Require-Python) -m pytest -q }
        default  { Get-Help $PSCommandPath; exit 1 }
    }
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
