param(
    [ValidateSet('start', 'stop', 'rebuild', 'status')]
    [string]$Action = 'start',
    [string]$BindAddress = '127.0.0.1',
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $projectRoot 'backend'
$runtimeDir = Join-Path $projectRoot 'data/launcher'
if ($Port -ne 8000) { $runtimeDir = Join-Path $runtimeDir $Port.ToString() }
$stateFile = Join-Path $runtimeDir 'processes.json'
$configFile = Join-Path $runtimeDir 'address.json'
if (-not $PSBoundParameters.ContainsKey('BindAddress') -and (Test-Path -LiteralPath $configFile)) {
    $BindAddress = (Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json).address
}
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
$state = @{}
$started = @()

function Get-OwnedProcess($record) {
    if (-not $record) { return $null }
    $process = Get-Process -Id $record.id -ErrorAction SilentlyContinue
    if ($process -and $process.StartTime.ToUniversalTime().Ticks.ToString() -eq $record.started -and
        $process.Path -eq $pythonExe) {
        return $process
    }
    return $null
}

function Save-State {
    $state | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8
}

function Stop-ServiceProcess([string]$role) {
    $process = Get-OwnedProcess $state[$role]
    if ($process) {
        # Stop only the recorded process and its descendants (venv Python redirector).
        $all = @(Get-CimInstance Win32_Process)
        $ids = @([int]$process.Id)
        for ($i = 0; $i -lt $ids.Count; $i++) {
            $ids += @($all | Where-Object { $_.ParentProcessId -eq $ids[$i] } |
                ForEach-Object { [int]$_.ProcessId })
        }
        [array]::Reverse($ids)
        foreach ($processId in $ids) {
            Stop-Process -Id $processId -ErrorAction SilentlyContinue
        }
        $process.WaitForExit(5000) | Out-Null
        Write-Host "Stopped $role."
    }
    $state.Remove($role)
    Save-State
}

function Start-ServiceProcess([string]$role, [string[]]$arguments) {
    if (Get-OwnedProcess $state[$role]) {
        Write-Host "$role is already running."
        return
    }
    $process = Start-Process -FilePath $pythonExe -ArgumentList $arguments -WorkingDirectory $backendDir `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimeDir "$role.out.log") `
        -RedirectStandardError (Join-Path $runtimeDir "$role.err.log")
    $state[$role] = @{ id = $process.Id; started = $process.StartTime.ToUniversalTime().Ticks.ToString() }
    Save-State
    $script:started += $role
}

function Build-Frontend {
    $frontendDir = Join-Path $projectRoot 'frontend'
    $inputs = @(Get-ChildItem -LiteralPath (Join-Path $frontendDir 'src') -File -Recurse)
    if (Test-Path -LiteralPath (Join-Path $frontendDir 'public')) {
        $inputs += @(Get-ChildItem -LiteralPath (Join-Path $frontendDir 'public') -File -Recurse)
    }
    $inputs += @(Get-ChildItem -LiteralPath $frontendDir -File | Where-Object { $_.Extension -ne '.tsbuildinfo' })
    $signature = ($inputs | Sort-Object FullName | ForEach-Object {
        $_.FullName.Substring($frontendDir.Length) + ':' + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    }) -join "`n"
    $stampFile = Join-Path $runtimeDir 'frontend-build.txt'
    $previous = if (Test-Path -LiteralPath $stampFile) { Get-Content -LiteralPath $stampFile -Raw } else { '' }
    if ($Action -ne 'rebuild' -and (Test-Path -LiteralPath (Join-Path $frontendDir 'dist/index.html')) -and
        $previous.TrimEnd() -eq $signature.TrimEnd()) {
        Write-Host 'Frontend is up to date.'
        return
    }
    # Standard installations first; also support this desktop's bundled Node/pnpm.
    $bundled = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies'
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        $env:Path = (Join-Path $bundled 'node/bin') + ';' + $env:Path
    }
    $pnpm = Get-Command pnpm.cmd -ErrorAction SilentlyContinue
    if (-not $pnpm) {
        $fallback = Join-Path $bundled 'bin/fallback/pnpm.cmd'
        if (Test-Path -LiteralPath $fallback) { $pnpm = Get-Item -LiteralPath $fallback }
    }
    if (-not $pnpm -or -not (Get-Command node -ErrorAction SilentlyContinue)) {
        throw 'Building requires Node.js and pnpm. Install them, then run start.cmd again.'
    }
    $pnpmPath = if ($pnpm -is [System.IO.FileInfo]) { $pnpm.FullName } else { $pnpm.Source }
    Write-Host 'Installing frontend dependencies and building...'
    & $pnpmPath --dir $frontendDir install --frozen-lockfile
    if ($LASTEXITCODE -ne 0) { throw 'pnpm install failed.' }
    & $pnpmPath --dir $frontendDir run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    $signature | Set-Content -LiteralPath $stampFile -Encoding UTF8
}

$digest = [System.Security.Cryptography.SHA256]::Create()
$lockName = 'Local\AiReviewLauncher-' + ([BitConverter]::ToString(
    $digest.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot))).Replace('-', ''))
$digest.Dispose()
$mutex = New-Object System.Threading.Mutex($false, $lockName)
$locked = $false
try {
    try { $locked = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Another launcher operation is running. Try again shortly.' }
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    if (Test-Path -LiteralPath $stateFile) {
        $saved = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
        foreach ($property in $saved.PSObject.Properties) { $state[$property.Name] = $property.Value }
    }
    if ($Action -eq 'status') {
        foreach ($role in @('api', 'worker')) {
            $status = if (Get-OwnedProcess $state[$role]) { 'running' } else { 'stopped' }
            Write-Host "${role}: $status"
        }
    } elseif ($Action -eq 'stop') {
        Stop-ServiceProcess 'worker'
        Stop-ServiceProcess 'api'
        Write-Host 'System stopped.'
    } else {
        if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Missing .venv. Follow the first-time setup in README.md.' }
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.env'))) { throw 'Missing .env. Follow the first-time setup in README.md.' }
        if ($Action -eq 'rebuild') {
            Stop-ServiceProcess 'worker'
            Stop-ServiceProcess 'api'
        }
        Build-Frontend
        if (-not (Get-OwnedProcess $state['api'])) {
            $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
            if ($listener) { throw "Port $Port is occupied. Stop the existing API before using this launcher." }
        }
        Start-ServiceProcess 'api' @('-m', 'uvicorn', 'app.main:app', '--host', $BindAddress, '--port', $Port.ToString())
        $ready = $false
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            if (-not (Get-OwnedProcess $state['api'])) { break }
            try {
                $health = Invoke-RestMethod "http://${BindAddress}:$Port/api/health" -TimeoutSec 1
                if ($health.status -eq 'ok') { $ready = $true; break }
            } catch { }
            Start-Sleep -Milliseconds 250
        }
        if (-not $ready) { throw 'API startup failed. See data/launcher/api.err.log.' }
        Start-ServiceProcess 'worker' @('-m', 'app.worker')
        Start-Sleep -Seconds 1
        if (-not (Get-OwnedProcess $state['worker'])) { throw 'Worker startup failed. See data/launcher/worker.err.log.' }
        @{ address = $BindAddress } | ConvertTo-Json | Set-Content -LiteralPath $configFile -Encoding UTF8
        Write-Host "System running: http://${BindAddress}:$Port"
        Write-Host 'API and Worker run in the background. Use stop.cmd to stop them.'
        Write-Host "Logs: $runtimeDir"
    }
} catch {
    foreach ($role in $started) { Stop-ServiceProcess $role }
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
