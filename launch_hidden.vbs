Option Explicit

If WScript.Arguments.Named.Exists("validate") Then WScript.Quit 0

Dim fso
Dim shell
Dim shellApp
Dim projectDir
Dim pythonwPath
Dim servicePath
Dim envPath
Dim serviceUrl
Dim stateUrl
Dim commandLine
Dim launchResult

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
Set shellApp = CreateObject("Shell.Application")

projectDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonwPath = fso.BuildPath(projectDir, ".venv\Scripts\pythonw.exe")
servicePath = fso.BuildPath(projectDir, "service.py")
envPath = fso.BuildPath(projectDir, ".env")
serviceUrl = "http://127.0.0.1:8900/"
stateUrl = "http://127.0.0.1:8900/api/state"

If Not fso.FileExists(pythonwPath) Then
    MsgBox "Python environment is not installed. Run install.bat first.", _
        vbCritical, "Preopen Recorder"
    WScript.Quit 1
End If

If Not fso.FileExists(servicePath) Then
    MsgBox "service.py is missing. Please restore the complete system folder.", _
        vbCritical, "Preopen Recorder"
    WScript.Quit 2
End If

If Not fso.FileExists(envPath) Then
    MsgBox "The built-in .env file is missing. Please contact the system provider.", _
        vbCritical, "Preopen Recorder"
    WScript.Quit 3
End If

shell.CurrentDirectory = projectDir

If Not IsServiceReady(stateUrl) Then
    commandLine = Quote(pythonwPath) & " " & Quote(servicePath) & _
        " --host 127.0.0.1 --port 8900"
    launchResult = shell.Run(commandLine, 0, False)
    If launchResult <> 0 Then
        MsgBox "Unable to start the background service (code " & _
            CStr(launchResult) & ").", vbCritical, "Preopen Recorder"
        WScript.Quit 4
    End If

    If Not WaitForService(stateUrl, 30) Then
        MsgBox "The service did not become ready within 30 seconds." & vbCrLf & _
            "Run install.bat again and check the installation messages.", _
            vbCritical, "Preopen Recorder"
        WScript.Quit 5
    End If
End If

shellApp.ShellExecute serviceUrl, "", "", "open", 1
WScript.Quit 0

Function Quote(ByVal value)
    Quote = Chr(34) & value & Chr(34)
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

Function WaitForService(ByVal url, ByVal timeoutSeconds)
    Dim attempt
    For attempt = 1 To timeoutSeconds * 4
        If IsServiceReady(url) Then
            WaitForService = True
            Exit Function
        End If
        WScript.Sleep 250
    Next
    WaitForService = False
End Function
