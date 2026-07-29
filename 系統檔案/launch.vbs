Option Explicit

If WScript.Arguments.Named.Exists("validate") Then WScript.Quit 0

Dim fso
Dim shell
Dim launcher

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
launcher = fso.BuildPath( _
    fso.GetParentFolderName(WScript.ScriptFullName), _
    "launch_hidden.vbs" _
)

If Not fso.FileExists(launcher) Then
    MsgBox "launch_hidden.vbs is missing. Please restore the complete system folder.", _
        vbCritical, "Preopen Recorder"
    WScript.Quit 1
End If

shell.Run "wscript.exe //nologo " & Chr(34) & launcher & Chr(34), 0, False
WScript.Quit 0
