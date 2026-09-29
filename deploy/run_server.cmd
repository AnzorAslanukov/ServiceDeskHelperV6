@echo off
REM ===========================================================================
REM Service Desk Helper - Self-Healing Server Launcher (REMOTE / production)
REM ===========================================================================
REM Invoked by the deploy scheduled task (see deploy.py step 7) so the server
REM runs detached from the SSH session and survives disconnect. All quoting
REM stays inside this file to avoid the shell-escaping problems of passing a
REM full command line through SSH.
REM
REM SELF-HEALING: uvicorn is run inside a restart loop. If uvicorn ever exits --
REM including the Windows ProactorEventLoop "WinError 64 (network name no longer
REM available)" listener-death that previously left a zombie process bound to
REM nothing on :8000 -- this script logs the exit, waits briefly, and relaunches
REM it. The process therefore never silently goes dead; it comes back on its own.
REM
REM STATUS DISPLAY: uvicorn's own stdout/stderr is redirected to logs\server.log
REM (so this window stays quiet). To show that it is alive WITHOUT spamming new
REM lines, we (1) keep a live heartbeat in the WINDOW TITLE (title updates never
REM scroll the buffer) and (2) rewrite ONE status line in place using a carriage
REM return. Only a restart ever prints an extra, permanent line.
REM ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion

REM Build a bare carriage-return (0x0D) character in CR. Printing it returns the
REM cursor to column 0 so the next text overwrites the current line in place.
for /f %%C in ('copy /Z "%~f0" nul') do set "CR=%%C"

set "PROJECT_DIR=C:\projects\service_desk_helper"
set "PYTHON=C:\Program Files\PyManager\python.exe"
set "LOG=%PROJECT_DIR%\logs\server.log"
set "URL=http://localhost:8000"
REM Seconds to wait before relaunching after uvicorn exits (avoids hot-looping
REM if it dies instantly, e.g. a bad import or the port still being released).
set "RESTART_DELAY=3"

cd /d "%PROJECT_DIR%"
if not exist "%PROJECT_DIR%\logs" mkdir "%PROJECT_DIR%\logs"

REM Runs=how many times uvicorn has been (re)started this session.
set /a RUNS=0

REM ---- One-time fixed banner (this is the only multi-line output) -----------
cls
echo ===========================================================================
echo   Service Desk Helper  --  SELF-HEALING SERVER
echo   URL : %URL%    (external: http://10.192.46.182:8000)
echo   Log : %LOG%
echo   Auto-restarts if the server ever stops. Close this window to stop.
echo ===========================================================================
echo(

:loop
set /a RUNS+=1
REM A stable, always-visible "it is running" indicator lives in the title bar;
REM updating the title never adds lines to the buffer.
title SDH RUNNING  ^|  starts=!RUNS!  ^|  %URL%

REM Rewrite a SINGLE status line in place. The leading <CR> (from the prompt
REM trick below) returns the cursor to column 0 so we overwrite, never append.
call :status "RUNNING since %date% %time%  (start #!RUNS!)  -- serving %URL%"

REM Foreground launch so we can detect the exit. Output goes to the log, so the
REM window itself stays on the single status line above.
"%PYTHON%" -m uvicorn src.main:app --host 0.0.0.0 --port 8000 >> "%LOG%" 2>&1

REM If we get here, uvicorn exited (crash, killed, or WinError 64 socket death).
set "EXITCODE=!ERRORLEVEL!"
title SDH RESTARTING  ^|  last exit=!EXITCODE!  ^|  %URL%
echo(
echo [%date% %time%] uvicorn exited (code !EXITCODE!). Restarting in %RESTART_DELAY%s...
echo [%date% %time%] uvicorn exited (code !EXITCODE!). Restarting in %RESTART_DELAY%s... >> "%LOG%"
timeout /t %RESTART_DELAY% /nobreak >nul
goto loop

REM ---- helper: print/overwrite one line without a trailing newline ----------
:status
REM <nul feeds set /p so it prints "%~1" with NO newline; the leading CR makes
REM it overwrite the current console line instead of scrolling.
<nul set /p "=.%~1                    "
<nul set /p "=!CR!"
goto :eof

