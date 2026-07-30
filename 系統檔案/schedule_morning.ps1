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

function Get-ServiceStateClassification {
    param(
        [AllowNull()]
        [object]$State,
        [bool]$ResponseReceived = $true
    )

    if (-not $ResponseReceived) {
        return "none"
    }
    if ($null -eq $State) {
        return "foreign"
    }

    $statusProperty = $State.PSObject.Properties["service_status"]
    if ($null -eq $statusProperty) {
        return "foreign"
    }

    $serviceStatus = ([string]$statusProperty.Value).Trim().ToLowerInvariant()
    $recordingProperty = $State.PSObject.Properties["recording"]
    $isRecording = (
        $null -ne $recordingProperty `
        -and $recordingProperty.Value -is [bool] `
        -and $recordingProperty.Value
    )
    if (
        $serviceStatus -in @("live", "armed") `
        -or $isRecording
    ) {
        return "live-recorder"
    }
    return "ours-not-live"
}

function Test-ServiceHealth {
    param([int]$HealthPort)

    try {
        $state = Invoke-RestMethod `
            -Uri "http://127.0.0.1:$HealthPort/api/state" `
            -TimeoutSec 2 `
            -ErrorAction Stop
        $classification = Get-ServiceStateClassification `
            -State $state `
            -ResponseReceived $true
        $statusProperty = if ($null -ne $state) {
            $state.PSObject.Properties["service_status"]
        }
        else {
            $null
        }
        $recordingProperty = if ($null -ne $state) {
            $state.PSObject.Properties["recording"]
        }
        else {
            $null
        }
        return [pscustomobject]@{
            Classification = $classification
            ServiceStatus = if ($null -ne $statusProperty) {
                [string]$statusProperty.Value
            }
            else {
                $null
            }
            Recording = (
                $null -ne $recordingProperty `
                -and $recordingProperty.Value -is [bool] `
                -and $recordingProperty.Value
            )
            Detail = $null
        }
    }
    catch {
        $responseReceived = $null -ne $_.Exception.Response
        return [pscustomobject]@{
            Classification = Get-ServiceStateClassification `
                -State $null `
                -ResponseReceived $responseReceived
            ServiceStatus = $null
            Recording = $false
            Detail = $_.Exception.Message
        }
    }
}

function Get-ListeningProcessIds {
    param([int]$ListenPort)

    if ($null -eq (
        Get-Command "Get-NetTCPConnection" -ErrorAction SilentlyContinue
    )) {
        throw "無法使用 Get-NetTCPConnection 安全判定 PORT $ListenPort 的擁有者"
    }
    try {
        return @(
            Get-NetTCPConnection `
                -LocalPort $ListenPort `
                -State Listen `
                -ErrorAction Stop |
                Where-Object { $_.OwningProcess -gt 0 } |
                Select-Object -ExpandProperty OwningProcess -Unique
        )
    }
    catch {
        if (
            $_.FullyQualifiedErrorId `
            -like "CmdletizationQuery_NotFound,Get-NetTCPConnection"
        ) {
            return @()
        }
        throw (
            "無法安全查詢 PORT $ListenPort 的監聽行程：" +
            $_.Exception.Message
        )
    }
}

function Test-ProcessIsDescendantOrSelf {
    param(
        [int]$CandidateProcessId,
        [int]$AncestorProcessId
    )

    $currentProcessId = $CandidateProcessId
    $visitedProcessIds = @{}
    for ($depth = 0; $depth -lt 16; $depth++) {
        if ($currentProcessId -eq $AncestorProcessId) {
            return $true
        }
        if (
            $currentProcessId -le 0 `
            -or $visitedProcessIds.ContainsKey($currentProcessId)
        ) {
            return $false
        }
        $visitedProcessIds[$currentProcessId] = $true
        try {
            $currentProcess = Get-CimInstance `
                -ClassName Win32_Process `
                -Filter "ProcessId = $currentProcessId" `
                -ErrorAction Stop
        }
        catch {
            throw (
                "無法確認監聽 PID=$CandidateProcessId 是否由 " +
                "PID=$AncestorProcessId 啟動：" +
                $_.Exception.Message
            )
        }
        if ($null -eq $currentProcess) {
            return $false
        }
        $currentProcessId = [int]$currentProcess.ParentProcessId
    }
    return $false
}

function Stop-OursNotLiveListener {
    param(
        [int]$ListenPort,
        [AllowEmptyString()]
        [string]$ServiceStatus
    )

    $displayStatus = if ([string]::IsNullOrWhiteSpace($ServiceStatus)) {
        "<empty>"
    }
    else {
        $ServiceStatus
    }
    $listenerPids = @(Get-ListeningProcessIds -ListenPort $ListenPort)
    if ($listenerPids.Count -eq 0) {
        return [pscustomobject]@{
            Action = "already-released"
            ProcessId = $null
            ServiceStatus = $displayStatus
        }
    }
    if ($listenerPids.Count -ne 1) {
        $message = (
            "LOUD：PORT $ListenPort 的本系統 $displayStatus 回應對應到多個" +
            "監聽 PID（$($listenerPids -join ',')），無法安全收回；" +
            "今日錄製未啟動。"
        )
        Write-Warning $message
        throw $message
    }

    $expectedPid = [int]$listenerPids[0]
    $confirmedHealth = Test-ServiceHealth -HealthPort $ListenPort
    $confirmedPids = @(Get-ListeningProcessIds -ListenPort $ListenPort)
    if ($confirmedHealth.Classification -eq "live-recorder") {
        return [pscustomobject]@{
            Action = "became-live"
            ProcessId = $expectedPid
            ServiceStatus = $confirmedHealth.ServiceStatus
        }
    }
    if (
        $confirmedHealth.Classification -eq "none" `
        -and $confirmedPids.Count -eq 0
    ) {
        return [pscustomobject]@{
            Action = "already-released"
            ProcessId = $null
            ServiceStatus = $displayStatus
        }
    }
    if ($confirmedHealth.Classification -ne "ours-not-live") {
        $message = (
            "LOUD：PORT $ListenPort 在收回前重新分類為 " +
            "$($confirmedHealth.Classification)，不會終止 PID；" +
            "今日錄製未啟動。"
        )
        Write-Warning $message
        throw $message
    }
    if (
        $confirmedPids.Count -ne 1 `
        -or [int]$confirmedPids[0] -ne $expectedPid
    ) {
        $message = (
            "LOUD：PORT $ListenPort 的監聽 PID 在收回前已變更" +
            "（原=$expectedPid；現=$($confirmedPids -join ',')），" +
            "不會終止不明行程；今日錄製未啟動。"
        )
        Write-Warning $message
        throw $message
    }

    $listenerProcess = Get-Process -Id $expectedPid -ErrorAction Stop
    $finalPids = @(Get-ListeningProcessIds -ListenPort $ListenPort)
    if (
        $finalPids.Count -ne 1 `
        -or [int]$finalPids[0] -ne $expectedPid
    ) {
        $message = (
            "LOUD：PORT $ListenPort 的監聽 PID 在終止前再次變更" +
            "（預期=$expectedPid；現=$($finalPids -join ',')），" +
            "不會終止不明行程；今日錄製未啟動。"
        )
        Write-Warning $message
        throw $message
    }
    $listenerProcess | Stop-Process -Force -ErrorAction Stop

    $releaseDeadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $releaseDeadline) {
        $remainingPids = @(
            Get-ListeningProcessIds -ListenPort $ListenPort
        )
        if ($remainingPids.Count -eq 0) {
            return [pscustomobject]@{
                Action = "reclaimed"
                ProcessId = $expectedPid
                ServiceStatus = $confirmedHealth.ServiceStatus
            }
        }
        Start-Sleep -Milliseconds 200
    }

    $message = (
        "LOUD：已終止本系統 $displayStatus 行程 PID=$expectedPid，" +
        "但 PORT $ListenPort 在 10 秒內未釋放；今日錄製未啟動。"
    )
    Write-Warning $message
    throw $message
}

function Start-CoordinatedService {
    $initialHealth = Test-ServiceHealth -HealthPort $Port
    $reclaimedStatus = $null
    switch ($initialHealth.Classification) {
        "live-recorder" {
            Write-Output (
                "service.py 已在 127.0.0.1:$Port live 錄製" +
                "（status=$($initialHealth.ServiceStatus)，" +
                "recording=$($initialHealth.Recording)）；不重複啟動。"
            )
            return
        }
        "ours-not-live" {
            $reclaimResult = Stop-OursNotLiveListener `
                -ListenPort $Port `
                -ServiceStatus $initialHealth.ServiceStatus
            switch ($reclaimResult.Action) {
                "became-live" {
                    Write-Output (
                        "service.py 在收回前已轉為 live 錄製" +
                        "（PID=$($reclaimResult.ProcessId)，" +
                        "status=$($reclaimResult.ServiceStatus)）；" +
                        "不重複啟動。"
                    )
                    return
                }
                "already-released" {
                    Write-Output (
                        "PORT $Port 上的本系統 " +
                        "$($reclaimResult.ServiceStatus) 非 live 錄製服務" +
                        "已自行釋放；將啟動真錄製。"
                    )
                }
                "reclaimed" {
                    $reclaimedStatus = $reclaimResult.ServiceStatus
                    Write-Output (
                        "PORT $Port 上為本系統 $reclaimedStatus " +
                        "非 live 錄製，已收回" +
                        "（PID=$($reclaimResult.ProcessId)）；" +
                        "將啟動真錄製。"
                    )
                }
                default {
                    throw "未知的 PORT 收回結果：$($reclaimResult.Action)"
                }
            }
        }
        "foreign" {
            $message = (
                "LOUD：PORT $Port 被非本系統程式占用，" +
                "無法啟動今日錄製，請釋放 PORT $Port。"
            )
            Write-Warning $message
            throw $message
        }
        "none" {
            # 沒有既有服務，直接走正常啟動流程。
        }
        default {
            throw "未知的服務健康分類：$($initialHealth.Classification)"
        }
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
        $launchHealth = Test-ServiceHealth -HealthPort $Port
        if (
            $launchHealth.Classification -in @(
                "live-recorder",
                "ours-not-live"
            )
        ) {
            $launchListenerPids = @(
                Get-ListeningProcessIds -ListenPort $Port
            )
            $launchListenerPid = if ($launchListenerPids.Count -eq 1) {
                [int]$launchListenerPids[0]
            }
            else {
                0
            }
            $listenerBelongsToLaunch = (
                $launchListenerPids.Count -eq 1 `
                -and (
                    Test-ProcessIsDescendantOrSelf `
                        -CandidateProcessId $launchListenerPid `
                        -AncestorProcessId $process.Id
                )
            )
            if (
                -not $listenerBelongsToLaunch
            ) {
                if (-not $process.HasExited) {
                    Stop-Process `
                        -Id $process.Id `
                        -Force `
                        -ErrorAction SilentlyContinue
                }
                $message = (
                    "LOUD：PORT $Port 的本系統回應不是由新啟動的 " +
                    "PID=$($process.Id) 或其子行程提供" +
                    "（監聽 PID=$($launchListenerPids -join ',')）；" +
                    "已停止新行程，今日錄製未啟動。"
                )
                Write-Warning $message
                throw $message
            }
            Set-Content `
                -LiteralPath $pidPath `
                -Value $process.Id `
                -Encoding ascii
            Write-Output (
                "service.py 啟動成功：PID=$($process.Id)；" +
                "UI=http://127.0.0.1:$Port/；" +
                "分類=$($launchHealth.Classification)；" +
                "status=$($launchHealth.ServiceStatus)；" +
                "recording=$($launchHealth.Recording)；" +
                "監聽 PID=$launchListenerPid；" +
                "錄製路徑=data\history\YYYYMMDD\"
            )
            if ($null -ne $reclaimedStatus) {
                Write-Output (
                    "PORT $Port 上原為 $reclaimedStatus 非 live 錄製，" +
                    "已收回並啟動真錄製服務。"
                )
            }
            return
        }
        if ($launchHealth.Classification -eq "foreign") {
            if (-not $process.HasExited) {
                Stop-Process `
                    -Id $process.Id `
                    -Force `
                    -ErrorAction SilentlyContinue
            }
            $message = (
                "LOUD：啟動等待期間 PORT $Port 出現非本系統回應；" +
                "已停止新行程，今日錄製未啟動。"
            )
            Write-Warning $message
            throw $message
        }
        Start-Sleep -Milliseconds 250
    }

    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    throw (
        "service.py 已啟動但 $LaunchTimeoutSeconds 秒內未通過 " +
        "live 錄製健康檢查；最後分類=$($launchHealth.Classification)，" +
        "status=$($launchHealth.ServiceStatus)，" +
        "recording=$($launchHealth.Recording)"
    )
}

function Set-ReliableTaskSettings {
    $getScheduledTask = Get-Command `
        "Get-ScheduledTask" `
        -ErrorAction SilentlyContinue
    $setScheduledTask = Get-Command `
        "Set-ScheduledTask" `
        -ErrorAction SilentlyContinue
    if (
        $null -ne $getScheduledTask `
        -and $null -ne $setScheduledTask
    ) {
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
        return
    }

    # 舊版環境可能沒有 ScheduledTasks 模組；用 Task Scheduler COM
    # 保留 WakeToRun / StartWhenAvailable，不把任務改回互動式登入。
    $scheduler = New-Object -ComObject "Schedule.Service"
    $scheduler.Connect()
    $rootFolder = $scheduler.GetFolder("\")
    $registeredTask = $rootFolder.GetTask($TaskName)
    $definition = $registeredTask.Definition
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $definition.Settings.WakeToRun = $true
    $definition.Settings.StartWhenAvailable = $true
    $principal = $definition.Principal
    $taskLogonS4U = 2
    if ([int]$principal.LogonType -ne $taskLogonS4U) {
        throw (
            "拒絕更新排程設定：現有 Principal.LogonType=" +
            "$($principal.LogonType)，不是 S4U。"
        )
    }
    $taskCreateOrUpdate = 6
    $rootFolder.RegisterTaskDefinition(
        $TaskName,
        $definition,
        $taskCreateOrUpdate,
        $principal.UserId,
        $null,
        $taskLogonS4U,
        $null
    ) | Out-Null
}

function Get-RegisteredTaskLogonType {
    $getScheduledTask = Get-Command `
        "Get-ScheduledTask" `
        -ErrorAction SilentlyContinue
    if ($null -ne $getScheduledTask) {
        $registeredTask = Get-ScheduledTask `
            -TaskName $TaskName `
            -ErrorAction Stop
        return [string]$registeredTask.Principal.LogonType
    }

    $scheduler = New-Object -ComObject "Schedule.Service"
    $scheduler.Connect()
    $rootFolder = $scheduler.GetFolder("\")
    $registeredTask = $rootFolder.GetTask($TaskName)
    $logonTypeNumber = [int]$registeredTask.Definition.Principal.LogonType
    $logonTypeNames = @{
        0 = "None"
        1 = "Password"
        2 = "S4U"
        3 = "InteractiveToken"
        4 = "Group"
        5 = "ServiceAccount"
        6 = "InteractiveTokenOrPassword"
    }
    if ($logonTypeNames.ContainsKey($logonTypeNumber)) {
        return $logonTypeNames[$logonTypeNumber]
    }
    return "Unknown($logonTypeNumber)"
}

if ($MyInvocation.InvocationName -eq ".") {
    return
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
        Write-Error $_ -ErrorAction Continue
        exit 2
    }
}

$escapedScriptPath = $ScriptPath.Replace('"', '""')
$taskPowerShellArguments = (
    "-NoProfile -ExecutionPolicy Bypass " +
    "-File `"$escapedScriptPath`" -Mode Execute -Port $Port"
)
$taskAction = (
    "powershell.exe $taskPowerShellArguments"
)

switch ($Mode) {
    "Register" {
        $currentUser = (
            [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        )
        if ([string]::IsNullOrWhiteSpace($currentUser)) {
            throw "無法取得目前 Windows 使用者，拒絕註冊排程。"
        }

        $requiredScheduledTaskCommands = @(
            "New-ScheduledTaskAction",
            "New-ScheduledTaskTrigger",
            "New-ScheduledTaskPrincipal",
            "New-ScheduledTaskSettingsSet",
            "Register-ScheduledTask"
        )
        $missingScheduledTaskCommands = @(
            $requiredScheduledTaskCommands |
                Where-Object {
                    $null -eq (
                        Get-Command $_ -ErrorAction SilentlyContinue
                    )
                }
        )
        $registrationMethod = $null
        if ($missingScheduledTaskCommands.Count -eq 0) {
            try {
                $scheduledAction = New-ScheduledTaskAction `
                    -Execute "powershell.exe" `
                    -Argument $taskPowerShellArguments `
                    -WorkingDirectory $ProjectDir
                $scheduledTrigger = New-ScheduledTaskTrigger `
                    -Weekly `
                    -WeeksInterval 1 `
                    -DaysOfWeek @(
                        "Monday",
                        "Tuesday",
                        "Wednesday",
                        "Thursday",
                        "Friday"
                    ) `
                    -At "08:25"
                $scheduledPrincipal = New-ScheduledTaskPrincipal `
                    -UserId $currentUser `
                    -LogonType S4U `
                    -RunLevel Limited
                $scheduledSettings = New-ScheduledTaskSettingsSet `
                    -AllowStartIfOnBatteries `
                    -DontStopIfGoingOnBatteries `
                    -WakeToRun `
                    -StartWhenAvailable
                Register-ScheduledTask `
                    -TaskName $TaskName `
                    -Action $scheduledAction `
                    -Trigger $scheduledTrigger `
                    -Principal $scheduledPrincipal `
                    -Settings $scheduledSettings `
                    -Force `
                    -ErrorAction Stop | Out-Null
                $registrationMethod = "ScheduledTasks/S4U"
            }
            catch {
                Write-Warning (
                    "ScheduledTasks S4U 註冊不可用，改用 schtasks " +
                    "/NP 免密碼備援；原因：$($_.Exception.Message)"
                )
            }
        }
        else {
            Write-Warning (
                "缺少 ScheduledTasks 指令（" +
                "$($missingScheduledTaskCommands -join ', ')），" +
                "改用 schtasks /NP 免密碼備援。"
            )
        }

        if ($null -eq $registrationMethod) {
            & schtasks.exe `
                /Create `
                /TN $TaskName `
                /SC WEEKLY `
                /D MON,TUE,WED,THU,FRI `
                /ST 08:25 `
                /TR $taskAction `
                /RU $currentUser `
                /NP `
                /RL LIMITED `
                /F
            if ($LASTEXITCODE -ne 0) {
                Write-Warning (
                    "無法建立免登入排程；未退回互動式任務。"
                )
                throw (
                    "排程免登入註冊失敗" +
                    "（schtasks /NP exit=$LASTEXITCODE）"
                )
            }
            $registrationMethod = "schtasks/NP"
        }

        Set-ReliableTaskSettings
        $registeredLogonType = Get-RegisteredTaskLogonType
        Write-Output (
            "排程 Principal.LogonType=$registeredLogonType；" +
            "註冊方式=$registrationMethod"
        )
        if ($registeredLogonType -ne "S4U") {
            Write-Warning (
                "排程未通過免登入驗證：" +
                "Principal.LogonType=$registeredLogonType；" +
                "不會靜默保留為互動式任務。"
            )
            throw (
                "排程 Principal.LogonType 必須為 S4U，" +
                "實際為 $registeredLogonType。"
            )
        }
        Write-Output (
            "已註冊：$TaskName（週一至週五 08:25；" +
            "service.py live；PORT $Port；免登入 S4U；" +
            "Limited；可用電池；WakeToRun；StartWhenAvailable）"
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
        $health = Test-ServiceHealth -HealthPort $Port
        $statusText = if (
            [string]::IsNullOrWhiteSpace($health.ServiceStatus)
        ) {
            "<none>"
        }
        else {
            $health.ServiceStatus
        }
        Write-Output (
            "服務健康分類=$($health.Classification)；" +
            "service_status=$statusText；recording=$($health.Recording)"
        )
    }
    "Unregister" {
        & schtasks.exe /Delete /TN $TaskName /F
        if ($LASTEXITCODE -ne 0) {
            throw "移除排程失敗（schtasks exit=$LASTEXITCODE）"
        }
    }
}
