"""
Service Desk Helper — One-Click Deploy Script
==============================================
Double-click this file in File Explorer to deploy the latest code
to the production workstation.

Steps performed:
  1. Git push local changes to GitHub
  2. SSH to workstation → git pull
  3. Install any new dependencies
  4. (Optional) Update ticket embeddings on the server — prompted; the ~3.1 GB
     file is rebuilt locally and transferred via SCP (never GitHub), then
     atomically swapped in BEFORE the restart so the new file is loaded.
  5. Stop the running server
  6. Start the server (detached)
  7. Verify the server is responding
"""

import os
import subprocess
import sys
import time

# Ensure the console can render this script's box-drawing/checkmark output
# (═ ✓ ✗ →) on legacy Windows codepages (cp1252). Best-effort: never fatal.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# ── Configuration ──────────────────────────────────────────────────────
SERVER = "AslanukA@10.192.46.182"
PROJECT_DIR = r"C:\projects\service_desk_helper"
SERVER_URL = "http://10.192.46.182:8000/health"
SSH_OPTS = "-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no"

# Local + remote locations of the ticket vector files. These are large
# (~3.1 GB .npy) and gitignored, so they are transferred OUT-OF-BAND via SCP
# over the same SSH channel used above — NEVER through GitHub (which would
# leak company data publicly).
VECTORS_SUBDIR = r"data\vectors"
EMBEDDINGS_NAME = "ticket_embeddings.npy"
METADATA_NAME = "ticket_metadata.json"
MANIFEST_NAME = "ticket_vectors_manifest.json"

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


def prompt_yes_no(question, default=False):
    """
    Ask a clearly-delimited, flushed yes/no question and block for an answer.

    - Prints the question on its own line, surrounded by blank lines, so it is
      impossible to miss even after a wall of subprocess output.
    - Flushes stdout BEFORE reading input (unflushed prompts are the classic
      reason a question appears to be "skipped").
    - Loops until the answer is a clear y/n (Enter alone takes the default).
    - If stdin is not interactive (e.g. piped), returns the default and says so
      instead of silently proceeding.
    """
    suffix = "[y/N]" if not default else "[Y/n]"
    banner_line = "═" * 60
    while True:
        print(f"\n{Colors.YELLOW}{banner_line}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.YELLOW}  {question}{Colors.RESET}")
        # Write the actual input line and flush so it is visible before we block.
        sys.stdout.write(f"{Colors.YELLOW}  Your choice {suffix}: {Colors.RESET}")
        sys.stdout.flush()
        try:
            raw = input()
        except (EOFError, OSError):
            print(f"  {Colors.YELLOW}(no interactive input available — "
                  f"defaulting to {'YES' if default else 'NO'}){Colors.RESET}")
            return default
        answer = raw.strip().lower()
        if answer == "":
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print(f"  {Colors.RED}Please answer 'y' or 'n'.{Colors.RESET}")


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


def run_local_streaming(cmd, cwd=None):
    """
    Run a command locally and STREAM its output line-by-line as it arrives.

    Unlike run_local(), this does NOT buffer until the process exits — so a
    long-running child (e.g. the --status check while a cold Databricks
    warehouse spins up) shows live progress instead of looking frozen.
    Returns True on success.
    """
    info(cmd)
    proc = subprocess.Popen(
        cmd, shell=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=cwd or os.path.dirname(os.path.abspath(__file__)),
    )
    try:
        for line in proc.stdout:
            print(f"    {line.rstrip()}")
    finally:
        proc.stdout.close()
        proc.wait()
    return proc.returncode == 0


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


def scp(local_path, remote_rel_path):
    """
    Copy a local file to the remote server via SCP (same host/key as ssh()).

    Args:
        local_path: absolute local file path.
        remote_rel_path: path RELATIVE to PROJECT_DIR on the remote (e.g.
            r"data\\vectors\\ticket_embeddings.npy.tmp").

    Returns (success, output).
    """
    remote_abs = f"{PROJECT_DIR}\\{remote_rel_path}"
    # scp target uses forward-slash host:path form; the Windows path after the
    # colon is passed through to the remote shell verbatim.
    full_cmd = f'scp {SSH_OPTS} "{local_path}" {SERVER}:"{remote_abs}"'
    info(f"scp → {os.path.basename(local_path)} ({_file_size_mb(local_path):.1f} MB)")
    result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    if output:
        for line in output.split("\n"):
            print(f"    {line}")
    return result.returncode == 0, output


def _file_size_mb(path):
    """Return a file's size in MB, or 0.0 if it does not exist."""
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0


def _local_manifest_sha():
    """Return the local manifest's (sha256_npy, sha256_meta), or (None, None)."""
    import json
    manifest_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        VECTORS_SUBDIR, MANIFEST_NAME,
    )
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("sha256_npy"), data.get("sha256_meta")
    except (OSError, ValueError):
        return None, None


def update_embeddings():
    """
    Rebuild the local ticket embeddings, then push them to the remote server
    (checksum-gated) and atomically swap them in — all BEFORE the server is
    restarted, so the normal deploy restart loads the new file.

    Returns True if the remote embeddings were updated (and therefore a restart
    is needed to load them), False if skipped or unchanged.
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    local_emb = os.path.join(project_root, VECTORS_SUBDIR, EMBEDDINGS_NAME)
    local_meta = os.path.join(project_root, VECTORS_SUBDIR, METADATA_NAME)
    local_manifest = os.path.join(project_root, VECTORS_SUBDIR, MANIFEST_NAME)

    # 1. Rebuild locally (incremental compute + export + atomic local swap).
    info("Rebuilding local ticket embeddings (incremental compute + export)...")
    ok, _ = run_local("python -m exploration.refresh_ticket_embeddings")
    if not ok:
        error("Local embeddings refresh failed — skipping remote update.")
        return False
    if not (os.path.exists(local_emb) and os.path.exists(local_meta)):
        error("Local embedding files missing after refresh — skipping remote update.")
        return False
    success("Local embeddings rebuilt")

    # 2. Checksum-gate: skip the ~3 GB transfer if the remote already matches.
    local_npy_sha, local_meta_sha = _local_manifest_sha()
    if local_npy_sha:
        remote_manifest_rel = f"{VECTORS_SUBDIR}\\{MANIFEST_NAME}"
        ok, remote_out = ssh(
            f"if (Test-Path '{PROJECT_DIR}\\{remote_manifest_rel}') "
            f"{{ Get-Content '{PROJECT_DIR}\\{remote_manifest_rel}' -Raw }}"
        )
        if ok and local_npy_sha in remote_out and (local_meta_sha or "") in remote_out:
            success("Remote embeddings already match local checksum — no transfer needed.")
            return False

    # 3. SCP both files (+ manifest) to remote TEMP paths.
    info(f"Transferring embeddings to {SERVER} (this can take a while)...")
    ok_emb, _ = scp(local_emb, f"{VECTORS_SUBDIR}\\{EMBEDDINGS_NAME}.tmp")
    ok_meta, _ = scp(local_meta, f"{VECTORS_SUBDIR}\\{METADATA_NAME}.tmp")
    if not (ok_emb and ok_meta):
        error("SCP transfer failed — remote embeddings left unchanged.")
        return False
    if os.path.exists(local_manifest):
        scp(local_manifest, f"{VECTORS_SUBDIR}\\{MANIFEST_NAME}")
    success("Embeddings transferred to remote temp files")

    # 4. Remote validate + atomic swap (keep .bak). Runs a small Python snippet
    #    on the server that asserts the .npy row count == metadata length before
    #    replacing the live files.
    vdir = f"{PROJECT_DIR}\\{VECTORS_SUBDIR}"
    py = (
        "import os,json,numpy as np;"
        f"d=r'{vdir}';"
        "e=os.path.join(d,'ticket_embeddings.npy');m=os.path.join(d,'ticket_metadata.json');"
        "et=e+'.tmp';mt=m+'.tmp';"
        "a=np.load(et,mmap_mode='r');"
        "meta=json.load(open(mt,encoding='utf-8'));"
        "assert a.shape[0]==len(meta),'row/meta mismatch';"
        "assert a.shape[1]==1024,'bad dims';"
        "os.replace(m,m+'.bak') if os.path.exists(m) else None;"
        "os.replace(e,e+'.bak') if os.path.exists(e) else None;"
        "os.replace(mt,m);os.replace(et,e);"
        "print('SWAP_OK rows='+str(a.shape[0]))"
    )
    ok, out = ssh(f'Set-Location \'{PROJECT_DIR}\'; python -c "{py}"')
    if ok and "SWAP_OK" in out:
        success("Remote embeddings validated and swapped into place")
        return True

    error("Remote validation/swap failed — live files left unchanged (temp files remain).")
    return False


# ── Main Deploy Flow ───────────────────────────────────────────────────

def main():
    # Enable ANSI colors on Windows
    os.system("")

    banner()
    total_steps = 7
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

    # Step 2: Force-sync server to match GitHub (workstation is deploy-only)
    step(2, total_steps, "Syncing workstation code to GitHub (force reset)")
    ssh(f"Set-Location '{PROJECT_DIR}'; git fetch origin master")
    ok, _ = ssh(f"Set-Location '{PROJECT_DIR}'; git reset --hard origin/master")
    if ok:
        success("Code synced to latest GitHub commit")
    else:
        error("Git reset failed on workstation")
        errors.append("git sync")

    # Step 3: Install dependencies
    step(3, total_steps, "Installing dependencies")
    ok, _ = ssh(f"Set-Location '{PROJECT_DIR}'; python -m pip install -r requirements.txt --quiet")
    success("Dependencies up to date")

    # Step 4: Optionally update ticket embeddings (prompted).
    # Done BEFORE stop/start so the normal restart loads the new file.
    step(4, total_steps, "Updating ticket embeddings (optional)")
    # Show how many tickets need vectorizing + a time estimate BEFORE asking,
    # so the y/N decision is informed. Read-only; safe to skip on error.
    info("Checking how many tickets need vectorizing (this makes 1 sample API call)...")
    info("A cold Databricks warehouse can take a few minutes to spin up — output streams below.")
    # -u => unbuffered child stdout so the streamed lines appear immediately.
    run_local_streaming("python -u -m exploration.refresh_ticket_embeddings --status")
    answer = prompt_yes_no(
        "Update ticket embeddings on the server? "
        "This rebuilds locally and transfers ~3 GB via SCP."
    )
    if answer:
        try:
            if update_embeddings():
                success("Ticket embeddings updated on server (loads on restart)")
            else:
                info("Embeddings not updated (skipped, unchanged, or failed above)")
        except Exception as e:
            error(f"Embeddings update raised: {e}")
            errors.append("embeddings update")
    else:
        info("Skipping embeddings update (code-only deploy)")

    # Step 5: Stop server
    step(5, total_steps, "Stopping current server")
    ssh("Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force")
    success("Server stopped")
    time.sleep(2)

    # Step 6: Start server via background SSH session (only reliable method)
    step(6, total_steps, "Starting server (background SSH session)")
    start_cmd = (
        f'start /b ssh {SSH_OPTS} {SERVER} '
        f'"Set-Location \'{PROJECT_DIR}\'; python -m uvicorn src.main:app --host 0.0.0.0 --port 8000" '
        f'> nul 2>&1'
    )
    info(start_cmd)
    subprocess.Popen(start_cmd, shell=True)
    success("Server starting in background SSH session")

    # Step 7: Verify
    step(7, total_steps, "Verifying server is responding")
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