param(
    [ValidateSet("Register", "Run", "Status", "Execute", "Unregister")]
    [string]$Mode = "Register",
    [switch]$RunNow,
    [ValidateRange(1, 65535)]
    [int]$Port = 8900
)

$ErrorActionPreference = "Stop"
$TaskName = "假試撮盤前監控"
$ProjectDir = $PSScriptRoot
$ScriptPath = $MyInvocation.MyCommand.Path
$ServicePath = Join-Path -Path $ProjectDir -ChildPath "service.py"
$LogDir = Join-Path -Path $ProjectDir -ChildPath "log"
$LaunchTimeoutSeconds = 20

function Resolve-Python {
    $localPython = Join-Path -Path $ProjectDir -ChildPath (
        ".venv\Scripts\python.exe"
    )
    if (Test-Path -LiteralPath $localPython -PathType Leaf) {
        return (Resolve-Path -LiteralPath $localPython).Path
    }
    $pythonCommand = Get-Command "python.exe" -ErrorAction Stop
    return $pythonCommand.Source
}

function Test-ServiceHealth {
    param([int]$HealthPort)

    try {
        $state = Invoke-RestMethod `
            -Uri "http://127.0.0.1:$HealthPort/api/state" `
            -TimeoutSec 2 `
            -ErrorAction Stop
        return (
            $null -ne $state `
            -and $null -ne $state.session `
            -and $null -ne $state.counts
        )
    }
    catch {
        return $false
    }
}

function Start-CoordinatedService {
    if (Test-ServiceHealth -HealthPort $Port) {
        Write-Output (
            "service.py 已在 127.0.0.1:$Port 提供 UI；" +
            "沿用同一程序負責當日錄製。"
        )
        return
    }

    $pythonPath = Resolve-Python
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    $launchKey = (Get-Date).ToString("yyyyMMdd-HHmmss")
    $stdoutPath = Join-Path -Path $LogDir -ChildPath (
        "service-$Port-$launchKey.out.log"
    )
    $stderrPath = Join-Path -Path $LogDir -ChildPath (
        "service-$Port-$launchKey.err.log"
    )
    $pidPath = Join-Path -Path $LogDir -ChildPath "service-$Port.pid"
    $serviceArguments = @(
        "service.py",
        "--host", "127.0.0.1",
        "--port", $Port.ToString(),
        "--session", "preopen"
    )

    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $process = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList $serviceArguments `
        -WorkingDirectory $ProjectDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru

    $deadline = (Get-Date).AddSeconds($LaunchTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            throw (
                "service.py 在 UI 健康檢查前結束" +
                "（exit=$($process.ExitCode)）"
            )
        }
        if (Test-ServiceHealth -HealthPort $Port) {
            Set-Content `
                -LiteralPath $pidPath `
                -Value $process.Id `
                -Encoding ascii
            Write-Output (
                "service.py 啟動成功：PID=$($process.Id)；" +
                "UI=http://127.0.0.1:$Port/；" +
                "live 錄製=data\history\YYYYMMDD\"
            )
            return
        }
        Start-Sleep -Milliseconds 250
    }

    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    throw (
        "service.py 已啟動但 $LaunchTimeoutSeconds 秒內未通過 " +
        "http://127.0.0.1:$Port/api/state 健康檢查"
    )
}

function Set-ReliableTaskSettings {
    $registeredTask = Get-ScheduledTask `
        -TaskName $TaskName `
        -ErrorAction Stop
    $settings = $registeredTask.Settings
    $settings.DisallowStartIfOnBatteries = $false
    $settings.StopIfGoingOnBatteries = $false
    $settings.WakeToRun = $true
    $settings.StartWhenAvailable = $true
    Set-ScheduledTask `
        -TaskName $TaskName `
        -Settings $settings `
        -ErrorAction Stop | Out-Null
}

if ($Mode -eq "Execute") {
    try {
        if (-not (Test-Path -LiteralPath $ServicePath -PathType Leaf)) {
            throw "找不到 service.py：$ServicePath"
        }
        Set-Location -LiteralPath $ProjectDir
        Start-CoordinatedService
        exit 0
    }
    catch {
        Write-Error $_
        exit 2
    }
}

$escapedScriptPath = $ScriptPath.Replace('"', '""')
$taskAction = (
    "powershell.exe -NoProfile -ExecutionPolicy Bypass " +
    "-File `"$escapedScriptPath`" -Mode Execute -Port $Port"
)

switch ($Mode) {
    "Register" {
        & schtasks.exe `
            /Create `
            /TN $TaskName `
            /SC WEEKLY `
            /D MON,TUE,WED,THU,FRI `
            /ST 08:25 `
            /TR $taskAction `
            /RL LIMITED `
            /F
        if ($LASTEXITCODE -ne 0) {
            throw "排程註冊失敗（schtasks exit=$LASTEXITCODE）"
        }
        Set-ReliableTaskSettings
        Write-Output (
            "已註冊：$TaskName（週一至週五 08:25；" +
            "service.py live；PORT $Port；可用電池並喚醒執行）"
        )
        if ($RunNow) {
            & schtasks.exe /Run /TN $TaskName
            if ($LASTEXITCODE -ne 0) {
                throw "排程立即執行失敗（schtasks exit=$LASTEXITCODE）"
            }
            Write-Output (
                "已送出 schtasks /Run；Action 會在 UI 健康後結束，" +
                "service.py 則保持常駐。"
            )
        }
    }
    "Run" {
        & schtasks.exe /Run /TN $TaskName
        if ($LASTEXITCODE -ne 0) {
            throw "排程立即執行失敗（schtasks exit=$LASTEXITCODE）"
        }
    }
    "Status" {
        & schtasks.exe /Query /TN $TaskName /V /FO LIST
        if ($LASTEXITCODE -ne 0) {
            throw "查詢排程失敗（schtasks exit=$LASTEXITCODE）"
        }
    }
    "Unregister" {
        & schtasks.exe /Delete /TN $TaskName /F
        if ($LASTEXITCODE -ne 0) {
            throw "移除排程失敗（schtasks exit=$LASTEXITCODE）"
        }
    }
}
