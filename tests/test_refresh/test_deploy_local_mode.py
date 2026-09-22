"""
Unit tests for deploy.py's LOCAL deploy option (deploy_local + helpers).

Scope: the target-selection dispatch (main routes to local vs remote based on
the yes/no answer), the shared health poll, and the local start/stop helpers —
all tested by monkeypatching the I/O boundary (prompt_yes_no, subprocess,
urllib) so no real network, process, or SSH/SCP work happens. This mirrors the
repo convention of unit-testing decision logic while leaving the live
process/HTTP behavior to manual testing.
"""

import deploy


# ── main() dispatch: local vs remote ───────────────────────────────────

def test_main_dispatches_to_remote_when_answer_no(monkeypatch):
    monkeypatch.setattr(deploy, "os", deploy.os)  # keep os intact
    monkeypatch.setattr(deploy, "banner", lambda: None)
    monkeypatch.setattr(deploy, "prompt_yes_no", lambda *a, **k: False)
    called = {"remote": 0, "local": 0}
    monkeypatch.setattr(deploy, "deploy_remote", lambda: called.__setitem__("remote", called["remote"] + 1))
    monkeypatch.setattr(deploy, "deploy_local", lambda: called.__setitem__("local", called["local"] + 1))
    # os.system("") is harmless; leave it.
    deploy.main()
    assert called == {"remote": 1, "local": 0}


def test_main_dispatches_to_local_when_answer_yes(monkeypatch):
    monkeypatch.setattr(deploy, "banner", lambda: None)
    monkeypatch.setattr(deploy, "prompt_yes_no", lambda *a, **k: True)
    called = {"remote": 0, "local": 0}
    monkeypatch.setattr(deploy, "deploy_remote", lambda: called.__setitem__("remote", called["remote"] + 1))
    monkeypatch.setattr(deploy, "deploy_local", lambda: called.__setitem__("local", called["local"] + 1))
    deploy.main()
    assert called == {"remote": 0, "local": 1}


# ── poll_health ────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status):
        self.status = status


def test_poll_health_returns_true_on_200(monkeypatch):
    monkeypatch.setattr(deploy.time, "sleep", lambda _s: None)
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=10: _Resp(200))
    healthy, err = deploy.poll_health("http://localhost:8000/health", timeout_seconds=5)
    assert healthy is True
    assert err is None


def test_poll_health_times_out_and_reports_last_error(monkeypatch):
    # Advance time so exactly one attempt runs, fails, then the deadline passes.
    #   call 1: deadline = time() + timeout = 0 + 10 = 10
    #   call 2: while-check 0 < 10  -> True  -> one urlopen attempt (fails)
    #   call 3: while-check 100 < 10 -> False -> loop exits, last_err retained
    ticks = iter([0, 0, 100])
    monkeypatch.setattr(deploy.time, "time", lambda: next(ticks))
    monkeypatch.setattr(deploy.time, "sleep", lambda _s: None)
    import urllib.request

    def _boom(url, timeout=10):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    healthy, err = deploy.poll_health("http://localhost:8000/health",
                                      timeout_seconds=10, interval_seconds=0)
    assert healthy is False
    assert "connection refused" in err


# ── start_local_server ─────────────────────────────────────────────────

def test_start_local_server_launches_the_local_cmd_detached(monkeypatch):
    captured = {}

    def _fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(deploy.subprocess, "Popen", _fake_popen)
    deploy.start_local_server()
    # The committed local launcher is what gets started.
    assert deploy.LOCAL_RUN_SERVER_CMD in captured["args"]
    # It is launched via a new console (`start`) and detached from this script.
    assert "start" in captured["args"]
    assert captured["kwargs"]["creationflags"] & 0x00000008  # DETACHED_PROCESS


# ── stop_local_server: PID parsing ─────────────────────────────────────

def test_stop_local_server_kills_listening_pid(monkeypatch):
    netstat_out = (
        "  TCP    0.0.0.0:8000     0.0.0.0:0      LISTENING       4321\r\n"
        "  TCP    127.0.0.1:8000   127.0.0.1:55010 ESTABLISHED    4321\r\n"
    )

    class _R:
        stdout = netstat_out

    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **k: _R())
    killed = []
    monkeypatch.setattr(deploy, "run_local", lambda cmd: (killed.append(cmd), (True, ""))[1])
    deploy.stop_local_server()
    assert any("taskkill /F /PID 4321" in c for c in killed)
    # PID 4321 appears twice in the netstat output but must be killed only once.
    assert len(killed) == 1


def test_stop_local_server_noop_when_nothing_on_port(monkeypatch):
    class _R:
        stdout = ""

    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **k: _R())
    killed = []
    monkeypatch.setattr(deploy, "run_local", lambda cmd: killed.append(cmd))
    deploy.stop_local_server()
    assert killed == []


# ── Step 4 gate: the slow --status scan must be gated by the up-front y/N ──

def _stub_deploy_local_io(monkeypatch, streamed):
    """
    Stub every I/O boundary deploy_local() touches so it can run in a test,
    recording each run_local_streaming command into ``streamed``. Returns nothing;
    callers set prompt_yes_no separately to drive the gate.
    """
    monkeypatch.setattr(deploy, "step", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "info", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "success", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "error", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "run_local", lambda *a, **k: (True, ""))
    monkeypatch.setattr(
        deploy, "run_local_streaming",
        lambda cmd, *a, **k: (streamed.append(cmd), True)[1],
    )
    # Neutralize the start/verify tail so no process/HTTP work happens.
    monkeypatch.setattr(deploy, "_deploy_local_start_and_verify", lambda *a, **k: None)


def test_local_gate_no_skips_the_status_scan(monkeypatch):
    streamed = []
    _stub_deploy_local_io(monkeypatch, streamed)
    # Gate answers NO (the only prompt_yes_no reached in this path).
    monkeypatch.setattr(deploy, "prompt_yes_no", lambda *a, **k: False)
    # If the scope prompt were ever reached it would be a bug; make it explode.
    monkeypatch.setattr(
        deploy, "prompt_ticket_limit",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("scope prompt reached")),
    )
    deploy.deploy_local()
    # The multi-minute --status scan must NOT have been run.
    assert not any("--status" in c for c in streamed)


def test_local_gate_yes_runs_the_status_scan(monkeypatch):
    streamed = []
    _stub_deploy_local_io(monkeypatch, streamed)
    # First prompt_yes_no (the gate) = YES, second (the confirm) = NO so we stop
    # before the scope prompt / actual rebuild.
    answers = iter([True, False])
    monkeypatch.setattr(deploy, "prompt_yes_no", lambda *a, **k: next(answers))
    deploy.deploy_local()
    # The scan runs exactly once now that the gate said YES.
    assert sum(1 for c in streamed if "--status" in c) == 1
