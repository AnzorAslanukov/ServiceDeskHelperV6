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
     file is rebuilt locally and TRANSFERRED via SCP (never GitHub) to *.tmp
     paths. A live transfer progress bar is shown. The atomic swap is DEFERRED
     to step 6 (after the server is stopped) — see note below.
  5. Stop the running server
  6. Apply the transferred embeddings: atomically swap the *.tmp files into
     place. This happens WHILE THE SERVER IS DOWN on purpose: the running
     server mmaps ticket_embeddings.npy, and Windows refuses to rename a
     memory-mapped file (WinError 32). Swapping after the stop removes that
     lock so the swap succeeds; the next start then loads the new file.
  7. Start the server (detached)
  8. Verify the server is responding
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


def parse_limit_choice(raw):
    """
    Pure parser for the "how many NEW tickets to embed" prompt. Returns a
    (kind, value) tuple so it is trivially unit-testable without any I/O:

        ""  / "a" / "all"     -> ("all", None)     # process the whole backlog
        a positive integer    -> ("limit", N)      # embed N, then finalize
        "c" / "cancel"        -> ("cancel", None)  # skip the embeddings update
        anything else,        -> ("invalid", None) # 0, negatives, non-numbers
        including "0"/"-5"/"x"                        (caller should re-prompt)
    """
    answer = (raw or "").strip().lower()
    if answer in ("", "a", "all"):
        return ("all", None)
    if answer in ("c", "cancel"):
        return ("cancel", None)
    try:
        n = int(answer)
    except ValueError:
        return ("invalid", None)
    if n <= 0:
        return ("invalid", None)
    return ("limit", n)


def prompt_ticket_limit(max_tries=3):
    """
    Ask how many NEW tickets to embed this run and block for an answer.

    Because the full backlog can take many hours, this lets the operator cap the
    run to a specific number of tickets (which finalizes/exports/ships normally,
    then stops) or process everything, or cancel the embeddings update entirely.

    Returns a (kind, value) tuple:
        ("all",    None) -> embed all new tickets
        ("limit",  N)    -> embed at most N new tickets
        ("cancel", None) -> skip the embeddings update

    Re-prompts on invalid input (0, negatives, non-numbers). After ``max_tries``
    invalid entries — or when stdin is not interactive — it defaults to CANCEL,
    the safe choice (no long, unattended run kicked off by accident).
    """
    banner_line = "═" * 60
    for _ in range(max_tries):
        print(f"\n{Colors.YELLOW}{banner_line}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.YELLOW}  How many NEW tickets to embed this run?{Colors.RESET}")
        print(f"{Colors.YELLOW}    [A]      All new tickets (may take many hours){Colors.RESET}")
        print(f"{Colors.YELLOW}    [number] Embed that many, then finalize (e.g. 5000){Colors.RESET}")
        print(f"{Colors.YELLOW}    [C]      Cancel — skip the embeddings update{Colors.RESET}")
        sys.stdout.write(f"{Colors.YELLOW}  Your choice [A/number/C]: {Colors.RESET}")
        sys.stdout.flush()
        try:
            raw = input()
        except (EOFError, OSError):
            print(f"  {Colors.YELLOW}(no interactive input available — "
                  f"defaulting to CANCEL){Colors.RESET}")
            return ("cancel", None)
        kind, value = parse_limit_choice(raw)
        if kind != "invalid":
            return (kind, value)
        print(f"  {Colors.RED}Please enter 'A' for all, a positive whole number, "
              f"or 'C' to cancel.{Colors.RESET}")
    print(f"  {Colors.RED}Too many invalid entries — defaulting to CANCEL.{Colors.RESET}")
    return ("cancel", None)


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
    Run a command locally and STREAM its output as it arrives, passing bytes
    through RAW so in-place progress bars (carriage-return '\\r' updates) render
    live instead of being withheld until the next newline.

    Unlike run_local(), this does NOT buffer until the process exits — so a
    long-running child (e.g. the --status check while a cold Databricks
    warehouse spins up, or the multi-hour embedding rebuild with its progress
    bar) shows live progress instead of looking frozen. Returns True on success.
    """
    info(cmd)
    import codecs
    # Binary stdout so we can forward '\r' and partial lines immediately.
    proc = subprocess.Popen(
        cmd, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=cwd or os.path.dirname(os.path.abspath(__file__)),
    )
    # Incremental decoder so multi-byte UTF-8 chars aren't split across reads.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while True:
            chunk = proc.stdout.read(64)
            if not chunk:
                break
            text = decoder.decode(chunk)
            if text:
                sys.stdout.write(text)
                sys.stdout.flush()
    finally:
        tail = decoder.decode(b"", final=True)
        if tail:
            sys.stdout.write(tail)
        proc.stdout.close()
        proc.wait()
    # Ensure the shell prompt / next log line starts cleanly after any bar.
    sys.stdout.write("\n")
    sys.stdout.flush()
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


def _fmt_bytes(num):
    """Human-readable size (e.g. '3.4 GB', '367.5 MB'). Robust to None/0."""
    n = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_eta(seconds):
    """
    Format an ETA in seconds as M:SS (or H:MM:SS for long transfers).

    Returns "--:--" when the ETA is unknown (None), negative, or implausibly
    large. The upper clamp (>= 100 hours) guards against the degenerate case
    where a decaying transfer rate approaches zero and (residual / rate) blows
    up to an astronomically large number — which previously printed a many-digit
    hour count and made a healthy transfer look broken.
    """
    if seconds is None or seconds < 0 or seconds >= 100 * 3600:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _remote_file_size(remote_rel_path):
    """
    Return the size in bytes of a file under PROJECT_DIR on the server, or None.

    Used to drive the SCP transfer progress bar by polling how many bytes have
    landed on the remote so far. Best-effort and quiet: any failure (file not
    yet created, transient SSH error) simply returns None so the caller skips
    that tick instead of breaking the transfer.
    """
    remote_abs = f"{PROJECT_DIR}\\{remote_rel_path}"
    cmd = (
        f"if (Test-Path '{remote_abs}') "
        f"{{ (Get-Item '{remote_abs}').Length }} else {{ '' }}"
    )
    full_cmd = f'ssh {SSH_OPTS} {SERVER} "{cmd}"'
    try:
        result = subprocess.run(
            full_cmd, shell=True, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    out = (result.stdout or "").strip()
    for token in out.split():
        if token.isdigit():
            return int(token)
    return None


def scp_streaming(local_path, remote_rel_path, poll_interval=4.0):
    """
    SCP a (large) file to the server while showing a live progress bar driven by
    polling the REMOTE file size — so it works on any OpenSSH/scp build,
    regardless of whether scp's own '\\r' progress meter reaches us through the
    pipe.

    A background thread polls the remote ``*.tmp`` size every ``poll_interval``
    seconds and redraws an in-place bar:

        ticket_embeddings.npy |####------| 42%  1.4/3.4 GB  22.1 MB/s  ETA 1:38

    Percent/ETA are computed against the known LOCAL file size. All polling is
    wrapped in best-effort guards: a failed size probe just skips that tick and
    never aborts the transfer. Returns (success, output) like scp().
    """
    import threading

    remote_abs = f"{PROJECT_DIR}\\{remote_rel_path}"
    full_cmd = f'scp {SSH_OPTS} "{local_path}" {SERVER}:"{remote_abs}"'
    total_bytes = int(_file_size_mb(local_path) * 1024 * 1024)
    name = os.path.basename(local_path)
    info(f"scp → {name} ({_fmt_bytes(total_bytes)})")

    # scp's own output is captured (hidden) so it can't fight our bar for the
    # terminal; our polled bar is the visible progress indicator.
    proc = subprocess.Popen(
        full_cmd, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    stop = threading.Event()
    start = time.time()
    bar_width = 30

    # Rates below this floor are treated as "unknown" for ETA purposes. Once a
    # transfer's tail stalls (bytes all sent, remote size static) the smoothed
    # rate decays toward — but never reaches — zero; without a floor, the ETA
    # (residual / rate) explodes. This keeps the ETA honest ("--:--") instead.
    min_rate = 1024.0  # 1 KB/s
    # Once the remote size reaches the full local size, the data is on the
    # server and scp is just finalizing (close/rename/fsync + final ACK). We
    # switch to a static "finalizing" line rather than recomputing a bogus ETA.
    done_bar = "#" * bar_width

    def _draw():
        last_seen, last_time = 0, start
        rate = 0.0
        last_frame = None          # only redraw when the visible text changes
        finalizing_shown = False
        while not stop.is_set():
            sent = _remote_file_size(remote_rel_path)
            now = time.time()
            if sent is not None and total_bytes > 0:
                elapsed = now - last_time
                if elapsed > 0 and sent >= last_seen:
                    inst = (sent - last_seen) / elapsed
                    # Smooth the rate a little so the ETA doesn't jitter wildly.
                    rate = inst if rate == 0 else (0.6 * rate + 0.4 * inst)
                last_seen, last_time = sent, now

                if sent >= total_bytes:
                    # Fully transferred; scp is finalizing. Show a stable line
                    # once, then stop redrawing so the tail can't flood output.
                    if not finalizing_shown:
                        sys.stdout.write(
                            f"\r    {name} |{done_bar}| 100%  "
                            f"{_fmt_bytes(total_bytes)}/{_fmt_bytes(total_bytes)}  "
                            f"finalizing...   "
                        )
                        sys.stdout.flush()
                        finalizing_shown = True
                else:
                    frac = min(sent / total_bytes, 1.0)
                    filled = int(bar_width * frac)
                    bar = "#" * filled + "-" * (bar_width - filled)
                    # Only produce an ETA when the rate is meaningful; a
                    # sub-floor rate is reported as unknown ("--:--").
                    eta = ((total_bytes - sent) / rate) if rate >= min_rate else None
                    frame = (
                        f"\r    {name} |{bar}| {frac * 100:4.0f}%  "
                        f"{_fmt_bytes(sent)}/{_fmt_bytes(total_bytes)}  "
                        f"{_fmt_bytes(rate)}/s  ETA {_fmt_eta(eta)}   "
                    )
                    if frame != last_frame:      # skip identical redraws
                        sys.stdout.write(frame)
                        sys.stdout.flush()
                        last_frame = frame
            stop.wait(poll_interval)

    drawer = threading.Thread(target=_draw, daemon=True)
    drawer.start()

    # Drain scp's (hidden) output so the pipe never blocks the child.
    try:
        captured = proc.stdout.read()
    finally:
        proc.stdout.close()
        proc.wait()
        stop.set()
        drawer.join(timeout=2)

    # Final line: show 100% on success, then a clean newline for the next log.
    if proc.returncode == 0 and total_bytes > 0:
        bar = "#" * bar_width
        sys.stdout.write(
            f"\r    {name} |{bar}| 100%  "
            f"{_fmt_bytes(total_bytes)}/{_fmt_bytes(total_bytes)}   \n"
        )
    else:
        sys.stdout.write("\n")
    sys.stdout.flush()

    output = ""
    if captured:
        try:
            output = captured.decode("utf-8", errors="replace").strip()
        except (UnicodeDecodeError, AttributeError):
            output = ""
        if output:
            for line in output.split("\n"):
                if "NativeCommandError" not in line and "CategoryInfo" not in line:
                    print(f"    {line}")
    return proc.returncode == 0, output


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


def stage_embeddings(limit=None):
    """
    Rebuild the local ticket embeddings and TRANSFER them to the server as
    ``*.tmp`` files (checksum-gated). It deliberately does NOT perform the
    atomic swap — that is deferred to ``apply_embeddings()``, which the deploy
    flow runs AFTER the server is stopped (step 6).

    Why defer the swap? On Windows the running server memory-maps
    ``ticket_embeddings.npy``, and a mapped file cannot be renamed/replaced
    (WinError 32). Swapping before the stop therefore fails every time; swapping
    after the stop succeeds. See the module docstring.

    When ``limit`` is a positive int, only that many NEW tickets are embedded in
    the Databricks compute step this run (via ``--limit``); the export + SCP
    transfer finalize normally on the partial result. Because the compute step
    skips already-embedded tickets, subsequent runs resume the remaining
    backlog. ``limit=None`` embeds the full backlog.

    Returns one of:
        "swap" — new (or stranded) temp files are staged and a swap is pending.
        "skip" — nothing to do (remote already up to date, or refresh failed in
                 a way that leaves live files correctly unchanged).
        "fail" — a hard error occurred (refresh crashed, files missing, or the
                 SCP transfer failed); no swap should be attempted.
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    local_emb = os.path.join(project_root, VECTORS_SUBDIR, EMBEDDINGS_NAME)
    local_meta = os.path.join(project_root, VECTORS_SUBDIR, METADATA_NAME)
    local_manifest = os.path.join(project_root, VECTORS_SUBDIR, MANIFEST_NAME)

    # 1. Rebuild locally (incremental compute + export + atomic local swap).
    #    Streamed with an unbuffered child (-u) so the in-place progress bar
    #    ("N/total") shows live during what can be a multi-hour embedding run.
    info("Rebuilding local ticket embeddings (incremental compute + export)...")
    if limit:
        info(f"Embedding at most {limit:,} new ticket(s) this run, then finalizing.")
    else:
        info("Embedding the FULL backlog; this can take a long time.")
    info("A live progress bar (with periodic ETA heartbeats) will appear below.")
    refresh_cmd = "python -u -m exploration.refresh_ticket_embeddings"
    if limit:
        refresh_cmd += f" --limit {int(limit)}"
    ok = run_local_streaming(refresh_cmd)
    if not ok:
        error("Local embeddings refresh failed — skipping remote update.")
        return "fail"
    if not (os.path.exists(local_emb) and os.path.exists(local_meta)):
        error("Local embedding files missing after refresh — skipping remote update.")
        return "fail"
    success("Local embeddings rebuilt")

    # 2. Checksum-gate: skip the ~3 GB transfer if the remote already matches.
    #    We ONLY skip when (a) the remote manifest matches the local checksum AND
    #    (b) the remote LIVE .npy actually has the manifest's row count. Condition
    #    (b) guards the case where a previous deploy transferred the files and
    #    updated the manifest, but the atomic swap failed — leaving the live files
    #    stale and the new data stranded in *.tmp. Without (b) the manifest would
    #    "match" and we'd skip forever, never applying the already-transferred data.
    local_npy_sha, local_meta_sha = _local_manifest_sha()
    if local_npy_sha:
        remote_manifest_rel = f"{VECTORS_SUBDIR}\\{MANIFEST_NAME}"
        ok, remote_out = ssh(
            f"if (Test-Path '{PROJECT_DIR}\\{remote_manifest_rel}') "
            f"{{ Get-Content '{PROJECT_DIR}\\{remote_manifest_rel}' -Raw }}"
        )
        manifest_matches = (
            ok and local_npy_sha in remote_out and (local_meta_sha or "") in remote_out
        )
        if manifest_matches:
            # Confirm the LIVE file is really swapped in (row count matches manifest).
            live_ok = _remote_live_matches_manifest()
            if live_ok:
                success("Remote embeddings already match local checksum — no transfer needed.")
                return "skip"
            info("Remote manifest matches, but live files are stale "
                 "(a previous swap did not complete) — will re-apply after stop.")
            # Skip the big re-transfer if the temp files are still present on the
            # server; the deferred swap (step 6) will reuse them. Otherwise fall
            # through to a normal transfer below.
            if _remote_temp_files_present():
                info("Reusing already-transferred temp files on the server.")
                return "swap"

    # 3. SCP both files (+ manifest) to remote TEMP paths. The two big files use
    #    scp_streaming() so a live transfer progress bar is shown; the tiny
    #    manifest uses plain scp() (no bar needed for a few KB).
    info(f"Transferring embeddings to {SERVER} (this can take a while)...")
    ok_emb, _ = scp_streaming(local_emb, f"{VECTORS_SUBDIR}\\{EMBEDDINGS_NAME}.tmp")
    ok_meta, _ = scp_streaming(local_meta, f"{VECTORS_SUBDIR}\\{METADATA_NAME}.tmp")
    if not (ok_emb and ok_meta):
        error("SCP transfer failed — remote embeddings left unchanged.")
        return "fail"
    if os.path.exists(local_manifest):
        scp(local_manifest, f"{VECTORS_SUBDIR}\\{MANIFEST_NAME}")
    success("Embeddings transferred to remote temp files")

    # Transfer done; the atomic swap is deferred to apply_embeddings() (step 6).
    return "swap"


def apply_embeddings():
    """
    Perform the deferred atomic swap of the transferred ``*.tmp`` embedding
    files into the live location. Call this ONLY after the server is stopped so
    the live ``.npy`` is no longer memory-mapped (otherwise Windows raises
    WinError 32 and the swap fails). Returns True on SWAP_OK.
    """
    return _remote_validate_and_swap()

    # 4. Remote validate + atomic swap (keep .bak). This runs a real repo script
    #    (scripts/swap_embeddings.py) that asserts the .npy row count == metadata
    #    length before replacing the live files. The script ships to the server
    #    via the git sync in Step 2, so it is guaranteed present here.
    #
    #    IMPORTANT: do NOT inline this as `python -c "..."`. ssh() wraps the whole
    #    remote command in double quotes, so an inner `-c "..."` closes them early
    #    and the remote PowerShell tries to parse the Python source (that was the
    #    "Missing argument in parameter list" swap failure). Passing the vectors
    #    dir as a single-quoted PowerShell argument keeps everything quote-safe.
    return _remote_validate_and_swap()


def _remote_validate_and_swap():
    """
    Run scripts/swap_embeddings.py on the server to validate the transferred
    *.tmp files and atomically swap them into place. Returns True on SWAP_OK.

    Quote-safety note: ssh() wraps the whole remote command in double quotes, so
    the vectors dir is passed as a SINGLE-quoted PowerShell argument (no inner
    double quotes) — this is what fixed the earlier "Missing argument in
    parameter list" PowerShell parse failure.
    """
    vdir = f"{PROJECT_DIR}\\{VECTORS_SUBDIR}"
    ok, out = ssh(
        f"Set-Location '{PROJECT_DIR}'; "
        f"python scripts\\swap_embeddings.py '{vdir}'"
    )
    if ok and "SWAP_OK" in out:
        success("Remote embeddings validated and swapped into place")
        return True

    error("Remote validation/swap failed — live files left unchanged (temp files remain).")
    return False


def _remote_temp_files_present():
    """True if BOTH transferred *.tmp embedding files exist on the server."""
    e = f"{PROJECT_DIR}\\{VECTORS_SUBDIR}\\{EMBEDDINGS_NAME}.tmp"
    m = f"{PROJECT_DIR}\\{VECTORS_SUBDIR}\\{METADATA_NAME}.tmp"
    ok, out = ssh(
        f"if ((Test-Path '{e}') -and (Test-Path '{m}')) "
        f"{{ 'TMP_PRESENT' }} else {{ 'TMP_MISSING' }}"
    )
    return ok and "TMP_PRESENT" in out


def _remote_live_matches_manifest():
    """
    True if the remote LIVE .npy row count equals the remote manifest's ``rows``.

    Used to detect a stranded state where the manifest was updated but the atomic
    swap did not complete, so the live files are still stale. Runs a tiny repo
    script over SSH (quote-safe, single-quoted arg — no inline python -c).
    """
    vdir = f"{PROJECT_DIR}\\{VECTORS_SUBDIR}"
    ok, out = ssh(
        f"Set-Location '{PROJECT_DIR}'; "
        f"python scripts\\check_live_matches_manifest.py '{vdir}'"
    )
    # Conservative: only treat an explicit LIVE_MATCH as "already applied".
    return ok and "LIVE_MATCH" in out


# ── Main Deploy Flow ───────────────────────────────────────────────────

def main():
    # Enable ANSI colors on Windows
    os.system("")

    banner()
    total_steps = 8
    errors = []
    # Whether stage_embeddings() left *.tmp files that still need to be swapped
    # into place. The swap itself is deferred to step 6 (after the server stop)
    # because Windows can't replace the memory-mapped .npy while the server runs.
    swap_pending = False

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
    # This only REBUILDS + TRANSFERS the files (to *.tmp). The atomic swap is
    # deferred to step 6 (after the server is stopped) so Windows will let us
    # replace the no-longer-mmapped live .npy.
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
        # Ask HOW MANY new tickets to embed this run — the full backlog can take
        # many hours, so allow capping to a number (then finalize) or cancelling.
        kind, limit = prompt_ticket_limit()
        if kind == "cancel":
            info("Skipping embeddings update (cancelled at scope prompt)")
        else:
            if kind == "limit":
                info(f"Embedding scope: up to {limit:,} new ticket(s) this run.")
            else:
                info("Embedding scope: ALL new tickets.")
            try:
                result = stage_embeddings(limit=limit)
                if result == "swap":
                    swap_pending = True
                    success("Embeddings staged on server — will apply after stop")
                elif result == "skip":
                    info("Embeddings not updated (remote already up to date)")
                else:  # "fail"
                    error("Embeddings staging failed — see messages above")
                    errors.append("embeddings staging")
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

    # Step 6: Apply staged embeddings (atomic swap) — WHILE THE SERVER IS DOWN.
    # The live .npy is no longer memory-mapped now, so os.replace can rename it
    # (this is the fix for the WinError 32 "file in use" swap failure that
    # happened when the swap ran before the stop).
    step(6, total_steps, "Applying new embeddings (atomic swap)")
    if swap_pending:
        try:
            if apply_embeddings():
                success("Embeddings swapped into place (loads on start)")
            else:
                error("Embeddings swap failed — live files left unchanged")
                errors.append("embeddings swap")
        except Exception as e:
            error(f"Embeddings swap raised: {e}")
            errors.append("embeddings swap")
    else:
        info("No embeddings to apply (skipped, unchanged, or staging failed)")

    # Step 7: Start server via background SSH session (only reliable method)
    step(7, total_steps, "Starting server (background SSH session)")
    start_cmd = (
        f'start /b ssh {SSH_OPTS} {SERVER} '
        f'"Set-Location \'{PROJECT_DIR}\'; python -m uvicorn src.main:app --host 0.0.0.0 --port 8000" '
        f'> nul 2>&1'
    )
    info(start_cmd)
    subprocess.Popen(start_cmd, shell=True)
    success("Server starting in background SSH session")

    # Step 8: Verify
    step(8, total_steps, "Verifying server is responding")
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