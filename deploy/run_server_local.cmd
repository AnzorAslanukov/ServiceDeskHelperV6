@echo off
REM Launches the Service Desk Helper server on THIS (local) machine. Invoked by
REM deploy.py's local-deploy path (deploy_local) when the remote workstation is
REM unavailable and SDH must run locally.
REM
REM Unlike deploy\run_server.cmd (which is triggered remotely via schtasks and
REM therefore hardcodes the workstation path), this file resolves its own project
REM root from its location (%~dp0 is ...\service_desk_helper\deploy\), so it works
REM regardless of where the repo is checked out.
REM
REM deploy.py starts this in a NEW, DETACHED console window so the server keeps
REM running after deploy.py exits; closing that window stops the server.
setlocal
set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"
if not exist "%PROJECT_DIR%\logs" mkdir "%PROJECT_DIR%\logs"
title Service Desk Helper (LOCAL) - http://localhost:8000
echo Starting Service Desk Helper locally on http://localhost:8000 ...
echo Logs: %PROJECT_DIR%\logs\server.log
echo Close this window to stop the server.
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000 > "%PROJECT_DIR%\logs\server.log" 2>&1
