' run_hidden.vbs — start run_local.bat with no console window.
' Used by the logon scheduled task so the pipeline never pops a prompt.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base
' Wait = True so Task Scheduler sees the real run duration: the task stays
' "Running" until the pipeline exits, MultipleInstances=IgnoreNew actually
' prevents an overlapping hourly trigger, and a failed run is visible in
' LastTaskResult instead of instantly reading as success.
sh.Run """" & base & "\run_local.bat""", 0, True
