Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\projects\service_desk_helper"
WshShell.Run "cmd /c python -m uvicorn src.main:app --host 0.0.0.0 --port 8000 > server.log 2> server_err.log", 0, False