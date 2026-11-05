Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
pythonPath = root & "\.venv\Scripts\python.exe"
logPath = root & "\bridge\server.log"

If Not fso.FileExists(pythonPath) Then
    MsgBox "Python interpreter not found: " & pythonPath, 16, "Oraimo Bridge"
    WScript.Quit 1
End If

shell.CurrentDirectory = root

command = "cmd.exe /c " & Chr(34) & _
    Chr(34) & pythonPath & Chr(34) & _
    " -u -m bridge >> " & Chr(34) & logPath & Chr(34) & _
    " 2>&1" & Chr(34)

shell.Run command, 0, False