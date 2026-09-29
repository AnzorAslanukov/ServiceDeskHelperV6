# Caddy Reverse Proxy — Elevated Setup Runbook

Goal: serve Service Desk Helper on **port 80** (for clients on the port‑80‑only
VPN) by running **Caddy** in front of the existing uvicorn app.

```
Browser (VPN, port 80)  ─▶  Caddy :80  ─▶  uvicorn 127.0.0.1:8000
```

uvicorn is **unchanged** — it still listens on `0.0.0.0:8000`. Caddy just adds a
port‑80 front door. The SDH app needs **no code changes** (its redirects/cookies
are already relative/port‑agnostic).

---

## What's already prepared (committed to the repo, no admin needed)

| File | Purpose |
|---|---|
| `deploy/caddy/Caddyfile` | Proxy config: `:80 → 127.0.0.1:8000`, WebSocket + streaming ready, plain HTTP (no auto‑HTTPS). |
| `deploy/get_caddy.ps1` | Downloads + SHA‑256‑verifies `caddy.exe` into `deploy/caddy/`. **Non‑admin.** |
| `deploy/run_caddy.cmd` | Self‑healing launcher (restart loop + non‑scrolling status), mirrors `run_server.cmd`. |

---

## Why elevation is required

A plain‑bind test on the server returned:
```
BIND_FAIL: [WinError 10013] An attempt was made to access a socket in a way
           forbidden by its access permissions
```
Port 80 is reserved by **HTTP.sys** (kernel, shows as `LISTENING PID 4 / System`).
`netsh http show urlacl` shows these `:80` reservations:
```
http://+:80/Temporary_Listen_Addresses/
http://+:80/0131501b-d67f-491b-9a40-c4bf27bcb4d4/
http://+:80/116B50EB-ECE2-41ac-8429-9F9E963361B7/
```
These belong to Windows system/management services (not IIS — IIS isn't
installed). Caddy binds via Winsock, so it **cannot** bind `:80` until HTTP.sys
releases it. Changing that requires **Administrator**.

---

## STEP 0 — Download Caddy (NON‑admin, can be done anytime)

On the server (any shell):
```powershell
powershell -ExecutionPolicy Bypass -File C:\projects\service_desk_helper\deploy\get_caddy.ps1
```
Expected: prints the Caddy version and `Caddyfile` validation `Valid configuration`.
Re‑run with `-Force` to re‑download.

---

## STEP 1 — (ELEVATED) Diagnose exactly what holds port 80

Open **PowerShell as Administrator**, then:
```powershell
Get-NetTCPConnection -LocalPort 80 -State Listen | Select-Object LocalAddress,OwningProcess
Get-Process -Id (Get-NetTCPConnection -LocalPort 80 -State Listen).OwningProcess
netsh http show urlacl | findstr /I ":80/"
sc.exe query HTTP
sc.exe enumdepend HTTP
```

---

## STEP 2 — (ELEVATED) Free port 80

Pick **ONE** approach. **B is the most reliable** on this host.

### Approach A — URL‑ACL grant (try first; may not be enough)
The root `http://+:80/` is unreserved, so granting it to the service account is
harmless. But Caddy uses Winsock, so if HTTP.sys holds an exclusive listen lock
this alone won't help — verify with STEP 3, else use Approach B.
```powershell
netsh http add urlacl url=http://+:80/ user="UPHS\AslanukA"
```

### Approach B — Stop the HTTP kernel service so Caddy can bind :80  ✅ reliable
> ⚠️ Stopping `HTTP` affects ALL HTTP.sys consumers (WinRM‑over‑HTTP, Web
> Management Service, WSD discovery). WinRM‑over‑HTTPS/5986 and RDP are
> unaffected. Do this in a maintenance window.
```powershell
sc.exe enumdepend HTTP          # see what will be affected first
Stop-Service -Name HTTP -Force  # stops dependents too (will prompt)
sc.exe config HTTP start= demand   # optional: keep it from auto-starting
Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction SilentlyContinue
netstat -ano | findstr ":80 "      # expect NO listener now
```

### Approach C — Keep HTTP.sys, use an HTTP.sys‑based proxy
If stopping `HTTP` is not allowed, don't use Caddy; use **IIS + ARR/URL Rewrite**
or a **.NET YARP** proxy (they register `http://+:80/` and coexist). Ask me to
prepare that variant instead.

---

## STEP 3 — (ELEVATED) Confirm the port is bindable
```powershell
& 'C:\Program Files\PyManager\python.exe' -c "import socket;s=socket.socket();s.bind(('0.0.0.0',80));s.listen(1);print('BIND_OK');s.close()"
```
- `BIND_OK` → proceed. `WinError 10013` → still held; redo STEP 2 (Approach B).

---

## STEP 4 — (ELEVATED) Open the Windows Firewall for port 80
```powershell
New-NetFirewallRule -DisplayName "SDH Caddy HTTP 80" -Direction Inbound `
  -Protocol TCP -LocalPort 80 -Action Allow -Profile Any
```

---

## STEP 5 — Start Caddy (self‑healing)

Quick foreground test:
```cmd
C:\projects\service_desk_helper\deploy\run_caddy.cmd
```
Detached via Task Scheduler (survives logoff), mirroring the app task:
```cmd
schtasks /Create /TN SDH_Caddy /TR "C:\projects\service_desk_helper\deploy\run_caddy.cmd" /SC ONCE /ST 00:00 /F
schtasks /Run    /TN SDH_Caddy
```

---

## STEP 6 — Verify end‑to‑end

From the server:
```powershell
Invoke-WebRequest http://localhost:80/health -UseBasicParsing | Select-Object StatusCode,Content
```
From a workstation **on the restrictive VPN** (the real test):
```
http://10.192.46.182/          → redirects to /login and renders
http://10.192.46.182/health    → {"status":"ok"}
```
Also exercise a WebSocket page (Feature #4 bulk assignment) and chatbot streaming
(`/chat`) to confirm live updates flow through the proxy.

---

## Rollback
```powershell
schtasks /End /TN SDH_Caddy 2>$null
schtasks /Delete /TN SDH_Caddy /F 2>$null
Get-Process caddy -ErrorAction SilentlyContinue | Stop-Process -Force
# If HTTP.sys was stopped/disabled and you want it back:
sc.exe config HTTP start= auto ; Start-Service HTTP
Remove-NetFirewallRule -DisplayName "SDH Caddy HTTP 80"
```
uvicorn on :8000 is untouched throughout, so the app stays reachable on
`http://10.192.46.182:8000` regardless of the proxy's state.

---

## Notes / decisions still open
- **Boot persistence:** want a `/SC ONSTART` task for Caddy (and to confirm the
  app task starts at boot)? Say so and I'll add it.
- **Security items** (weak `SESSION_SECRET_KEY`, tracked `cookies.txt`) are
  deferred per your instruction — worth doing before this is widely exposed.

PID **4 = System** confirms HTTP.sys owns the port (expected).
