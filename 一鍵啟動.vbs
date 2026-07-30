Option Explicit

If WScript.Arguments.Named.Exists("validate") Then WScript.Quit 0

Dim fso
Dim shell
Dim shellApp
Dim rootDir
Dim systemFolderName
Dim systemDir
Dim venvDir
Dim venvPythonPath
Dim pythonwPath
Dim installer
Dim launcher
Dim installMarker
Dim logDir
Dim installLog
Dim lockPath
Dim stateUrl
Dim lockOwned
Dim mainResult
Dim runtimeError
Dim runtimeDescription

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
Set shellApp = CreateObject("Shell.Application")

rootDir = fso.GetParentFolderName(WScript.ScriptFullName)
systemFolderName = ChrW(&H7CFB) & ChrW(&H7D71) & _
    ChrW(&H6A94) & ChrW(&H6848)
systemDir = fso.BuildPath(rootDir, systemFolderName)
venvDir = fso.BuildPath(systemDir, ".venv")
venvPythonPath = fso.BuildPath(venvDir, "Scripts\python.exe")
pythonwPath = fso.BuildPath(venvDir, "Scripts\pythonw.exe")
installer = fso.BuildPath(systemDir, "install.bat")
launcher = fso.BuildPath(systemDir, "launch_hidden.vbs")
installMarker = fso.BuildPath(systemDir, ".install_complete")
logDir = fso.BuildPath(systemDir, "log")
installLog = fso.BuildPath(logDir, "install.log")
lockPath = fso.BuildPath(logDir, "one_click.lock")
stateUrl = "http://127.0.0.1:8900/api/state"
lockOwned = False

On Error Resume Next
Err.Clear
mainResult = Main()
runtimeError = Err.Number
runtimeDescription = Err.Description
Err.Clear
On Error GoTo 0
ReleaseLock
If runtimeError <> 0 Then
    MsgBox "The launcher stopped unexpectedly (code " & _
        CStr(runtimeError) & ")." & vbCrLf & runtimeDescription & vbCrLf & _
        "Run it again; if it still fails, review log\install.log.", _
        vbCritical, "Preopen Recorder"
    WScript.Quit 99
End If
WScript.Quit mainResult

Function Main()
    Dim needsInstall
    Dim installCommand
    Dim installResult
    Dim launchCommand
    Dim launchResult

    Main = 1

    If Not fso.FolderExists(systemDir) Then
        MsgBox "The system folder is missing. Restore the complete package.", _
            vbCritical, "Preopen Recorder"
        Exit Function
    End If

    If Not fso.FileExists(launcher) Then
        MsgBox "launch_hidden.vbs is missing. Restore the complete system folder.", _
            vbCritical, "Preopen Recorder"
        Main = 2
        Exit Function
    End If

    If IsServiceReady(stateUrl) Then
        launchCommand = "wscript.exe //nologo " & Quote(launcher)
        shell.Run launchCommand, 0, False
        Main = 0
        Exit Function
    End If

    needsInstall = (Not fso.FileExists(installMarker)) Or _
        (Not IsVenvReady(venvPythonPath))

    If needsInstall And Not IsAdministrator() And _
        Not WScript.Arguments.Named.Exists("elevationAttempted") Then
        If RelaunchElevated() Then
            Main = 0
            Exit Function
        End If
        ' UAC was declined or unavailable. Continue without elevation so the
        ' existing scheduler logic can register its Interactive fallback.
    End If

    If Not EnsureLogFolder() Then
        MsgBox "Unable to create the local log folder.", _
            vbCritical, "Preopen Recorder"
        Main = 3
        Exit Function
    End If

    If Not AcquireLock() Then
        shell.Popup _
            "Installation or startup is already running. " & _
            "The front end will open automatically when it is ready.", _
            5, "Preopen Recorder", vbInformation
        Main = 0
        Exit Function
    End If

    needsInstall = (Not fso.FileExists(installMarker)) Or _
        (Not IsVenvReady(venvPythonPath))
    If needsInstall Then
        If Not fso.FileExists(installer) Then
            MsgBox "install.bat is missing. Restore the complete system folder.", _
                vbCritical, "Preopen Recorder"
            Main = 4
            Exit Function
        End If

        shell.Popup _
            "First-time setup is running and may take several minutes. " & _
            "No action is needed; the front end will open automatically.", _
            5, "Preopen Recorder", vbInformation

        shell.Environment("PROCESS")("INSTALL_NO_PAUSE") = "1"
        installCommand = "cmd.exe /d /s /c " & _
            Quote(Quote(installer) & " >" & Quote(installLog) & " 2>&1")
        installResult = shell.Run(installCommand, 0, True)

        If installResult <> 0 Then
            MsgBox "Installation did not complete successfully (code " & _
                CStr(installResult) & ")." & vbCrLf & _
                "Please install Python 3.12 first if it is missing." & vbCrLf & _
                "Details were saved to:" & vbCrLf & installLog, _
                vbCritical, "Preopen Recorder"
            Main = 5
            Exit Function
        End If

        If Not fso.FileExists(installMarker) Or _
            Not fso.FileExists(pythonwPath) Or _
            Not IsVenvReady(venvPythonPath) Then
            MsgBox "Installation finished without a usable Python 3.12 " & _
                "environment." & vbCrLf & _
                "Please install Python 3.12 first, then try again." & vbCrLf & _
                "Details were saved to:" & vbCrLf & installLog, _
                vbCritical, "Preopen Recorder"
            Main = 6
            Exit Function
        End If
    End If

    launchCommand = "wscript.exe //nologo " & Quote(launcher)
    launchResult = shell.Run(launchCommand, 0, True)
    If launchResult <> 0 Then
        MsgBox "The front end could not be started (code " & _
            CStr(launchResult) & ")." & vbCrLf & _
            "Review the local log folder without sharing .env.", _
            vbCritical, "Preopen Recorder"
        Main = 7
        Exit Function
    End If

    Main = 0
End Function

Function IsAdministrator()
    Dim command
    Dim result

    command = "powershell.exe -NoLogo -NoProfile -NonInteractive " & _
        "-WindowStyle Hidden -Command " & Quote( _
        "$p=New-Object Security.Principal.WindowsPrincipal(" & _
        "[Security.Principal.WindowsIdentity]::GetCurrent());" & _
        "if($p.IsInRole(" & _
        "[Security.Principal.WindowsBuiltInRole]::Administrator))" & _
        "{exit 0}else{exit 1}")
    result = shell.Run(command, 0, True)
    IsAdministrator = (result = 0)
End Function

Function RelaunchElevated()
    Dim arguments

    RelaunchElevated = False
    arguments = "//nologo " & Quote(WScript.ScriptFullName) & _
        " /elevationAttempted"

    On Error Resume Next
    Err.Clear
    shellApp.ShellExecute "wscript.exe", arguments, "", "runas", 0
    If Err.Number = 0 Then
        RelaunchElevated = True
    Else
        Err.Clear
    End If
    On Error GoTo 0
End Function

Function EnsureLogFolder()
    On Error Resume Next
    If Not fso.FolderExists(logDir) Then
        fso.CreateFolder logDir
    End If
    EnsureLogFolder = (Err.Number = 0 And fso.FolderExists(logDir))
    Err.Clear
    On Error GoTo 0
End Function

Function AcquireLock()
    Dim lockFile

    AcquireLock = False
    If fso.FileExists(lockPath) Then
        If Not IsLockStale() Then Exit Function
        On Error Resume Next
        fso.DeleteFile lockPath, True
        Err.Clear
        On Error GoTo 0
    End If

    On Error Resume Next
    Err.Clear
    Set lockFile = fso.CreateTextFile(lockPath, False, True)
    If Err.Number <> 0 Then
        Err.Clear
        On Error GoTo 0
        Exit Function
    End If
    lockFile.WriteLine CStr(Now)
    lockFile.Close
    On Error GoTo 0

    lockOwned = True
    AcquireLock = True
End Function

Function IsLockStale()
    Dim lockFile

    IsLockStale = False
    On Error Resume Next
    Set lockFile = fso.GetFile(lockPath)
    If Err.Number <> 0 Then
        Err.Clear
        On Error GoTo 0
        Exit Function
    End If
    IsLockStale = (DateDiff("n", lockFile.DateLastModified, Now) >= 120)
    On Error GoTo 0
End Function

Sub ReleaseLock()
    If Not lockOwned Then Exit Sub

    On Error Resume Next
    If fso.FileExists(lockPath) Then
        fso.DeleteFile lockPath, True
    End If
    lockOwned = False
    Err.Clear
    On Error GoTo 0
End Sub

Function IsVenvReady(ByVal pythonPath)
    Dim command
    Dim result

    IsVenvReady = False
    If Not fso.FileExists(pythonPath) Then Exit Function

    command = Quote(pythonPath) & _
        " -c " & Quote( _
        "import sys,struct;raise SystemExit(0 if " & _
        "sys.version_info[:2]==(3,12) and " & _
        "struct.calcsize('P')*8==64 else 1)")
    result = shell.Run(command, 0, True)
    IsVenvReady = (result = 0)
End Function

Function IsServiceReady(ByVal url)
    Dim request

    On Error Resume Next
    Set request = CreateObject("MSXML2.XMLHTTP.6.0")
    request.Open "GET", url, False
    request.setRequestHeader "Cache-Control", "no-cache"
    request.Send
    IsServiceReady = (Err.Number = 0 And request.Status >= 200 And _
        request.Status < 300)
    Err.Clear
    On Error GoTo 0
End Function

Function Quote(ByVal value)
    Quote = Chr(34) & value & Chr(34)
End Function
