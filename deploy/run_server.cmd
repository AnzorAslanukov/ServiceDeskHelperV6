@echo off
REM Launches the Service Desk Helper server. Invoked by the deploy scheduled
REM task (see deploy.py step 7) so the server runs detached from the SSH session
REM and survives disconnect. All quoting stays inside this file to avoid the
REM shell-escaping problems of passing a full command line through SSH.
cd /d "C:\projects\service_desk_helper"
if not exist "C:\projects\service_desk_helper\logs" mkdir "C:\projects\service_desk_helper\logs"
"C:\Program Files\PyManager\python.exe" -m uvicorn src.main:app --host 0.0.0.0 --port 8000 > "C:\projects\service_desk_helper\logs\server.log" 2>&1
