"""The /ping every role image answers.

A detached session must keep working, and a finished one must be allowed to end. The
first health server answered `Healthy` with the current time on every ping, so it
never told the Runtime the agent was working, and its timestamp claimed a state change
on every ping. These tests pin the AgentCore SDK's contract instead: HealthyBusy while
the agent CLI runs, and a timestamp that moves only when the status changes.
"""
from __future__ import annotations

import importlib.util
import json
import re
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("healthcheck", ROOT / "coding-agents" / "healthcheck.py")
healthcheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(healthcheck)

ROLES = ("claude-code", "claude-code-validator", "codex", "kiro", "opencode")


def _proc(tmp_path, processes):
    for pid, (comm, argv, *state) in processes.items():
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
        (d / "stat").write_text(f"{pid} ({comm}) {state[0] if state else 'S'} 1 1 1\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (tmp_path / "self").mkdir()
    return str(tmp_path)


def test_the_agent_cli_is_found_by_name_or_by_the_script_node_runs(tmp_path):
    proc = _proc(tmp_path, {
        1: ("python3", ["python3", "/app/healthcheck.py"]),
        7: ("bash", ["bash", "/app/run.sh"]),
        9: ("node", ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"]),
    })
    assert healthcheck.agent_running({"claude", "claude-code"}, proc)
    assert not healthcheck.agent_running({"codex"}, proc)


def test_a_path_or_argument_that_merely_mentions_a_cli_is_not_busy(tmp_path):
    """Only the program, or an interpreter's script and its npm package, name a process."""
    proc = _proc(tmp_path, {
        3: ("tail", ["tail", "-f", "/mnt/s3files/claude/log"]),
        4: ("python3", ["python3", "/work/codex/run.py"]),
        5: ("bash", ["bash", "-c", "cd /mnt/s3files/codex && ls"]),
        6: ("grep", ["grep", "-r", "claude-code", "/home/agent"]),
    })
    assert not healthcheck.agent_running({"claude", "claude-code", "codex"}, proc)


def test_the_npm_launchers_of_each_cli_are_found(tmp_path):
    cases = {
        ("node", "/usr/local/bin/claude"): "claude",
        ("node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"): "claude-code",
        ("node", "/usr/lib/node_modules/@openai/codex/bin/codex.js"): "codex",
        ("/usr/bin/node", "/usr/local/bin/opencode"): "opencode",
    }
    for i, ((prog, script), name) in enumerate(cases.items()):
        proc = _proc(tmp_path / str(i), {40 + i: ("node", [prog, script])}) \
            if (tmp_path / str(i)).mkdir() is None else None
        assert healthcheck.agent_running({name}, proc), (prog, script)


def test_a_native_cli_and_a_vanished_process_are_handled(tmp_path):
    proc = _proc(tmp_path, {12: ("kiro-cli-chat", ["/home/agent/.local/bin/kiro-cli-chat", "chat"])})
    assert healthcheck.agent_running({"kiro-cli", "kiro-cli-chat"}, proc)
    (tmp_path / "12" / "cmdline").unlink()
    assert healthcheck.agent_running({"kiro-cli-chat"}, proc), "comm alone is enough"
    for f in (tmp_path / "12").iterdir():
        f.unlink()
    assert not healthcheck.agent_running({"kiro-cli-chat"}, proc), "an exited process is not busy"


def test_an_exited_cli_left_as_a_zombie_is_not_busy(tmp_path):
    """Live, October 13: Claude Code started with nohup exited, was reparented to the
    health server (PID 1), and stayed a zombie named claude, so /ping said HealthyBusy
    for as long as the session lived."""
    proc = _proc(tmp_path, {272: ("claude", [], "Z"), 7: ("otelcol-contrib", ["/usr/local/bin/otelcol-contrib"])})
    assert not healthcheck.agent_running({"claude", "claude-code"}, proc)
    proc2 = _proc(tmp_path / "live", {273: ("claude", ["claude", "--print"], "R")}) if (tmp_path / "live").mkdir() is None else None
    assert healthcheck.agent_running({"claude"}, proc2)


def test_reaping_is_only_for_pid_1():
    healthcheck.reap()  # not PID 1 here: a no-op, never an error


def test_the_timestamp_moves_only_when_the_status_changes():
    now = [1000.0]
    running = [False]
    status = healthcheck.Status({"claude"}, clock=lambda: now[0], probe=lambda busy: running[0])
    assert status.read() == ("Healthy", 1000)
    now[0] = 1600
    assert status.read() == ("Healthy", 1000), "an idle agent stays idle since it went idle"
    running[0] = True
    now[0] = 1700
    assert status.read() == ("HealthyBusy", 1700)
    now[0] = 2500
    assert status.read() == ("HealthyBusy", 1700)
    running[0] = False
    now[0] = 2600
    assert status.read() == ("Healthy", 2600)


def test_no_busy_names_means_never_busy():
    status = healthcheck.Status(set(), probe=lambda busy: True)
    assert status.read()[0] == "Healthy"


def test_the_server_answers_the_runtime_contract():
    status = healthcheck.Status({"claude"}, clock=lambda: 42, probe=lambda busy: True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), healthcheck.make_handler("claude-code", status))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        body = json.load(urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/ping"))
    finally:
        server.shutdown()
    assert body["status"] == "HealthyBusy"
    assert body["time_of_last_update"] == 42
    assert body["agent"] == "claude-code"


def test_every_role_image_runs_the_shared_server_with_its_own_cli():
    expected = {"claude-code": {"claude"}, "claude-code-validator": {"claude"},
                "codex": {"codex"}, "kiro": {"kiro-cli-chat"}, "opencode": {"opencode"}}
    for role in ROLES:
        text = (ROOT / "coding-agents" / role / "Dockerfile").read_text()
        assert "--from=toolchain healthcheck.py /app/healthcheck.py" in text, role
        assert not (ROOT / "coding-agents" / role / "healthcheck.py").exists(), \
            f"{role} must not carry its own copy of the health server"
        cmd = re.search(r'^CMD (\[.*\])$', text, re.M)
        argv = json.loads(cmd.group(1))
        assert argv[:4] == ["python3", "/app/healthcheck.py", "--agent", role], role
        busy = {argv[i + 1] for i, a in enumerate(argv) if a == "--busy"}
        assert expected[role] <= busy, (role, busy)
