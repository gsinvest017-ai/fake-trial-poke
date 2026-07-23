param(
    [ValidateSet("Register", "Run", "Status", "Execute", "Unregister")]
    [string]$Mode = "Register",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$TaskName = "假試撮盤前監控"
$ProjectDir = $PSScriptRoot
$ScriptPath = $MyInvocation.MyCommand.Path
$RunnerPath = Join-Path -Path $ProjectDir -ChildPath "run_session.py"

function Resolve-Python {
    $localPython = Join-Path -Path $ProjectDir -ChildPath ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $localPython) {
        return (Resolve-Path -LiteralPath $localPython).Path
    }
    $pythonCommand = Get-Command "python.exe" -ErrorAction Stop
    return $pythonCommand.Source
}

function Invoke-SessionAction {
    $pythonPath = Resolve-Python
    Set-Location -LiteralPath $ProjectDir

    $now = Get-Date
    $isWeekday = $now.DayOfWeek -notin @(
        [System.DayOfWeek]::Saturday,
        [System.DayOfWeek]::Sunday
    )
    $liveStart = $now.Date.AddHours(8).AddMinutes(20)
    $liveEnd = $now.Date.AddHours(9)

    if ($isWeekday -and $now -ge $liveStart -and $now -lt $liveEnd) {
        & $pythonPath $RunnerPath --session preopen
    }
    else {
        Write-Output "目前不在盤前啟動區間；改跑完全離線 sample，驗證排程動作、路徑與 dashboard 串接。"
        & $pythonPath $RunnerPath --session preopen --sample
    }
    exit $LASTEXITCODE
}

if ($Mode -eq "Execute") {
    Invoke-SessionAction
}

if (-not (Test-Path -LiteralPath $RunnerPath)) {
    throw "找不到 run_session.py：$RunnerPath"
}

$escapedScriptPath = $ScriptPath.Replace('"', '""')
$taskAction = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$escapedScriptPath`" -Mode Execute"

switch ($Mode) {
    "Register" {
        & schtasks.exe /Create /TN $TaskName /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:25 /TR $taskAction /RL LIMITED /F
        if ($LASTEXITCODE -ne 0) {
            throw "排程註冊失敗（schtasks exit=$LASTEXITCODE）"
        }
        Write-Output "已註冊：$TaskName（週一至週五 08:25）"
        if ($RunNow) {
            & schtasks.exe /Run /TN $TaskName
            if ($LASTEXITCODE -ne 0) {
                throw "排程立即執行失敗（schtasks exit=$LASTEXITCODE）"
            }
            Write-Output "已送出 schtasks /Run；非盤前時段會自動跑離線 sample 驗證。"
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
