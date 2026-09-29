@echo off
REM ===========================================================================
REM Service Desk Helper - Self-Healing Caddy Reverse Proxy Launcher (REMOTE)
REM ===========================================================================
REM Runs Caddy in front of the uvicorn app: port 80 -> 127.0.0.1:8000, so
REM clients on the port-80-only VPN can reach SDH. Mirrors deploy\run_server.cmd:
REM
REM SELF-HEALING: caddy runs inside a restart loop. If it ever exits (crash,
REM kill, or a transient bind failure), this script logs the exit, waits briefly,
REM and relaunches -- the proxy never silently stays down.
REM
REM STATUS DISPLAY: caddy's own stdout/stderr is redirected to
REM logs\caddy.log (so this window stays quiet). "It is running" is shown WITHOUT
REM spamming new lines via (1) a live WINDOW TITLE heartbeat and (2) a single
REM in-place status line rewritten with a carriage return. Only a restart prints
REM an extra permanent line.
REM
REM PREREQUISITE: port 80 must be bindable. On this Windows server HTTP.sys holds
REM :80, so the elevated steps in deploy\CADDY_RUNBOOK.md must be done first, or
REM every start will loop on a WinError 10013 bind failure (visible in caddy.log).
REM ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion

REM Build a bare carriage-return (0x0D) char so we can overwrite one line in place.
for /f %%C in ('copy /Z "%~f0" nul') do set "CR=%%C"

set "PROJECT_DIR=C:\projects\service_desk_helper"
set "CADDY_EXE=%PROJECT_DIR%\deploy\caddy\caddy.exe"
set "CADDYFILE=%PROJECT_DIR%\deploy\caddy\Caddyfile"
set "LOG=%PROJECT_DIR%\logs\caddy.log"
set "URL=http://10.192.46.182  (port 80 -> 127.0.0.1:8000)"
REM Seconds to wait before relaunching after caddy exits (avoids hot-looping,
REM e.g. when port 80 is not yet free and every start fails instantly).
set "RESTART_DELAY=3"

cd /d "%PROJECT_DIR%"
if not exist "%PROJECT_DIR%\logs" mkdir "%PROJECT_DIR%\logs"

set /a RUNS=0

REM ---- One-time fixed banner (the only multi-line output) -------------------
cls
echo ===========================================================================
echo   Service Desk Helper  --  SELF-HEALING CADDY REVERSE PROXY
echo   Front door : http://10.192.46.182   (port 80)
echo   Upstream   : http://127.0.0.1:8000  (uvicorn)
echo   Log        : %LOG%
echo   Auto-restarts if Caddy stops. Close this window to stop the proxy.
echo ===========================================================================
echo(

if not exist "%CADDY_EXE%" (
	echo ERROR: caddy.exe not found at "%CADDY_EXE%".
	echo Run deploy\get_caddy.ps1 on the server first to download it.
	echo ERROR: caddy.exe missing at %CADDY_EXE% >> "%LOG%"
	timeout /t 10 /nobreak >nul
	exit /b 1
)

:loop
set /a RUNS+=1
title SDH-CADDY RUNNING  ^|  starts=!RUNS!  ^|  port 80

call :status "PROXY RUNNING since %date% %time%  (start #!RUNS!)  -- 80 -> 127.0.0.1:8000"

REM Foreground launch so we can detect the exit. --config points at the
REM Caddyfile; --adapter caddyfile parses it. Output goes to the log.
"%CADDY_EXE%" run --config "%CADDYFILE%" --adapter caddyfile >> "%LOG%" 2>&1

set "EXITCODE=!ERRORLEVEL!"
title SDH-CADDY RESTARTING  ^|  last exit=!EXITCODE!  ^|  port 80
echo(
echo [%date% %time%] caddy exited (code !EXITCODE!). Restarting in %RESTART_DELAY%s...
echo [%date% %time%] caddy exited (code !EXITCODE!). Restarting in %RESTART_DELAY%s... >> "%LOG%"
timeout /t %RESTART_DELAY% /nobreak >nul
goto loop

REM ---- helper: print/overwrite one line without a trailing newline ----------
:status
<nul set /p "=.%~1                    "
<nul set /p "=!CR!"
goto :eof
