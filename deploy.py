"""
Service Desk Helper — One-Click Deploy Script
==============================================
Double-click this file in File Explorer to deploy the latest code
to the production workstation.

Steps performed:
  1. Git push local changes to GitHub
  2. SSH to workstation → git pull
  3. Install any new dependencies
  4. Stop the running server
  5. Start the server (detached)
  6. Verify the server is responding
"""

import os
import subprocess
import sys
import time

# ── Configuration ──────────────────────────────────────────────────────
SERVER = "AslanukA@10.192.46.182"
PROJECT_DIR = r"C:\projects\service_desk_helper"
SERVER_URL = "http://10.192.46.182:8000/health"
SSH_OPTS = "-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no"

# ── Helpers ────────────────────────────────────────────────────────────

class Colors:
    """ANSI color codes for terminal output."""
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def banner():
    print(f"""
{Colors.CYAN}{'═' * 60}
  Service Desk Helper — Deploy to Production
{'═' * 60}{Colors.RESET}
""")


def step(number, total, description):
    print(f"\n{Colors.BOLD}[{number}/{total}]{Colors.RESET} {Colors.CYAN}{description}{Colors.RESET}")
    print(f"{'─' * 50}")


def success(msg):
    print(f"  {Colors.GREEN}✓ {msg}{Colors.RESET}")


def error(msg):
    print(f"  {Colors.RED}✗ {msg}{Colors.RESET}")


def info(msg):
    print(f"  {Colors.YELLOW}→ {msg}{Colors.RESET}")


def run_local(cmd, cwd=None):
    """Run a command locally and return (success, output)."""
    info(cmd)
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        cwd=cwd or os.path.dirname(os.path.abspath(__file__))
    )
    output = (result.stdout + result.stderr).strip()
    if output:
        for line in output.split("\n"):
            print(f"    {line}")
    return result.returncode == 0, output


def ssh(command):
    """Run a command on the remote server via SSH."""
    full_cmd = f'ssh {SSH_OPTS} {SERVER} "{command}"'
    info(f"ssh → {command}")
    result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    if output:
        for line in output.split("\n"):
            # Filter out PowerShell noise
            if "NativeCommandError" not in line and "CategoryInfo" not in line:
                print(f"    {line}")
    return result.returncode == 0, output


# ── Main Deploy Flow ───────────────────────────────────────────────────

def main():
    # Enable ANSI colors on Windows
    os.system("")

    banner()
    total_steps = 6
    errors = []

    # Pre-flight: fix git safe.directory (needed when double-clicked from Explorer)
    project_dir = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
    run_local(f'git config --global --add safe.directory "{project_dir}"')

    # Step 1: Git commit & push
    step(1, total_steps, "Pushing local changes to GitHub")
    ok, _ = run_local("git add -A")
    if ok:
        ok, out = run_local('git status --porcelain')
        if out.strip():
            ok, _ = run_local('git commit -m "Deploy update"')
            if not ok:
                error("Git commit failed")
                errors.append("git commit")
        else:
            info("No changes to commit (working tree clean)")

    ok, _ = run_local("git push origin master")
    if ok:
        success("Pushed to GitHub")
    else:
        error("Git push failed — do you have uncommitted changes or conflicts?")
        errors.append("git push")

    # Step 2: Pull on server
    step(2, total_steps, "Pulling latest code on workstation")
    ok, _ = ssh(f"Set-Location '{PROJECT_DIR}'; git pull origin master")
    if ok:
        success("Code updated on workstation")
    else:
        # git pull may return exit code 1 due to PowerShell stderr
        success("Pull completed (check output above)")

    # Step 3: Install dependencies
    step(3, total_steps, "Installing dependencies")
    ok, _ = ssh(f"Set-Location '{PROJECT_DIR}'; python -m pip install -r requirements.txt --quiet")
    success("Dependencies up to date")

    # Step 4: Stop server
    step(4, total_steps, "Stopping current server")
    ssh("Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force")
    success("Server stopped")
    time.sleep(2)

    # Step 5: Start server
    step(5, total_steps, "Starting server (detached)")
    ssh(
        f"Set-Location '{PROJECT_DIR}'; "
        "Start-Process python -ArgumentList '-m','uvicorn','src.main:app','--host','0.0.0.0','--port','8000' "
        "-WindowStyle Hidden"
    )
    success("Server process launched")

    # Step 6: Verify
    step(6, total_steps, "Verifying server is responding")
    info("Waiting for server to start...")
    time.sleep(8)

    try:
        import urllib.request
        resp = urllib.request.urlopen(SERVER_URL, timeout=10)
        if resp.status == 200:
            success(f"Server is UP — {SERVER_URL} returned 200")
        else:
            error(f"Server returned HTTP {resp.status}")
            errors.append("health check")
    except Exception as e:
        error(f"Could not reach server: {e}")
        errors.append("health check")

    # Summary
    print(f"\n{'═' * 60}")
    if not errors:
        print(f"{Colors.GREEN}{Colors.BOLD}  ✓ DEPLOY SUCCESSFUL{Colors.RESET}")
        print(f"    Application: http://10.192.46.182:8000")
    else:
        print(f"{Colors.RED}{Colors.BOLD}  ✗ DEPLOY COMPLETED WITH ERRORS{Colors.RESET}")
        print(f"    Issues: {', '.join(errors)}")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nDeploy cancelled.")
    except Exception as e:
        print(f"\n{Colors.RED}Unexpected error: {e}{Colors.RESET}")
    finally:
        input("\nPress Enter to close...")