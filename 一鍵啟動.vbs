Option Explicit

If WScript.Arguments.Named.Exists("validate") Then WScript.Quit 0

Dim fso
Dim shell
Dim rootDir
Dim systemFolderName
Dim systemDir
Dim venvDir
Dim pythonwPath
Dim installer
Dim launcher
Dim installCommand
Dim installResult

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

rootDir = fso.GetParentFolderName(WScript.ScriptFullName)
systemFolderName = ChrW(&H7CFB) & ChrW(&H7D71) & _
    ChrW(&H6A94) & ChrW(&H6848)
systemDir = fso.BuildPath(rootDir, systemFolderName)
venvDir = fso.BuildPath(systemDir, ".venv")
pythonwPath = fso.BuildPath(venvDir, "Scripts\pythonw.exe")
installer = fso.BuildPath(systemDir, "install.bat")
launcher = fso.BuildPath(systemDir, "launch_hidden.vbs")

If Not fso.FolderExists(systemDir) Then
    MsgBox "The system folder is missing. Restore the complete package.", _
        vbCritical, "Preopen Recorder"
    WScript.Quit 1
End If

If Not fso.FileExists(pythonwPath) Then
    If Not fso.FileExists(installer) Then
        MsgBox "install.bat is missing. Restore the complete system folder.", _
            vbCritical, "Preopen Recorder"
        WScript.Quit 2
    End If

    installCommand = "cmd.exe /d /c " & Chr(34) & Chr(34) & _
        installer & Chr(34) & Chr(34)
    installResult = shell.Run(installCommand, 1, True)

    If Not fso.FileExists(pythonwPath) Then
        MsgBox "Installation could not create the Python environment." & _
            vbCrLf & "Install 64-bit Python 3.12, then try again.", _
            vbCritical, "Preopen Recorder"
        WScript.Quit 3
    End If

    If installResult <> 0 Then
        MsgBox "Installation did not complete successfully (code " & _
            CStr(installResult) & ")." & vbCrLf & _
            "Open the system folder and run install.bat again.", _
            vbCritical, "Preopen Recorder"
        WScript.Quit 4
    End If
End If

If Not fso.FileExists(launcher) Then
    MsgBox "launch_hidden.vbs is missing. Please restore the complete system folder.", _
        vbCritical, "Preopen Recorder"
    WScript.Quit 5
End If

shell.Run "wscript.exe //nologo " & Chr(34) & launcher & Chr(34), 0, False
WScript.Quit 0
