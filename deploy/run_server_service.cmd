@echo off
REM ===========================================================================
REM Service Desk Helper - HEADLESS Server Launcher (scheduled task / service)
REM ===========================================================================
REM Purpose: run uvicorn with NO interactive-console dependencies, so it works
REM when launched by a scheduled task that runs "whether the user is logged on
REM or not" (i.e. in a non-interactive / Session 0 context).
REM
REM Why this exists (separate from deploy\run_server.cmd):
REM   deploy\run_server.cmd is the INTERACTIVE launcher -- it uses cls, a carriage
REM   -return trick, a live window title, and an in-place status line. Those need
REM   a real console. When a task is set to "Run whether user is logged on or
REM   not", there is no console, those console ops misbehave, and the batch can
REM   exit with STATUS_CONTROL_C_EXIT (0xC000013A / schtasks Last Result
REM   -1073741510) BEFORE it ever reaches the uvicorn line. This file has ZERO
REM   console tricks, so it runs headless reliably.
REM
REM EVENT LOOP FIX: uvicorn is launched with "--loop asyncio:SelectorEventLoop".
REM On Windows single-process mode uvicorn defaults to the ProactorEventLoop,
REM whose IOCP accept() loop can die with "WinError 64 (the specified network
REM name is no longer available)" when a client / load-balancer probe drops a
REM connection abruptly -- leaving the listener dead on :8000 while the process
REM stays alive. The SelectorEventLoop avoids that. See the README Windows note.
REM
REM SELF-HEALING: uvicorn runs inside a restart loop. If it ever exits for any
REM reason, this script logs the exit (to the file log, since there is no window),
REM waits briefly, and relaunches it -- so the server never silently stays dead.
REM
REM Checkout-agnostic: resolves PROJECT_DIR from this file's own location
REM (%~dp0 = ...\service_desk_helper\deploy\), so it works wherever the repo is.
REM ===========================================================================
setlocal EnableExtensions

REM Resolve the project root from this script's location (deploy\ -> repo root).
set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"

REM Prefer the committed production Python if present; otherwise fall back to
REM whatever "python" is on PATH for the task's run-as user. Both are known to
REM run SDH on this server.
set "PYTHON=C:\Program Files\PyManager\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

set "LOG=%PROJECT_DIR%\logs\server.log"
set "RESTART_DELAY=3"

if not exist "%PROJECT_DIR%\logs" mkdir "%PROJECT_DIR%\logs"

:loop
echo [%date% %time%] Starting uvicorn (SelectorEventLoop) on 0.0.0.0:8000 >> "%LOG%"
"%PYTHON%" -m uvicorn src.main:app --host 0.0.0.0 --port 8000 --loop asyncio:SelectorEventLoop >> "%LOG%" 2>&1
echo [%date% %time%] uvicorn exited (code %ERRORLEVEL%). Restarting in %RESTART_DELAY%s... >> "%LOG%"
timeout /t %RESTART_DELAY% /nobreak >nul
goto loop
