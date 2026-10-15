"""session.py: one name maps to one runtimeSessionId, and each command runs in a shell."""
from __future__ import annotations

import importlib.util
import json
import shlex
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "coding-agents" / "session.py"
spec = importlib.util.spec_from_file_location("workshop_session_cli", SOURCE)
session = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = session
spec.loader.exec_module(session)

ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/claude_code-AbC"


class FakeClient:
    def __init__(self):
        self.calls, self.stopped = [], []

    def invoke_agent_runtime_command(self, **kw):
        self.calls.append(kw)
        return {"stream": [{"chunk": {"contentDelta": {"stdout": "UP 31\nhello\n"}}},
                           {"chunk": {"contentStop": {"exitCode": 0}}}]}

    def stop_runtime_session(self, **kw):
        self.stopped.append(kw["runtimeSessionId"])


def setup(tmp_path, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(session, "STATE_DIR", tmp_path)
    monkeypatch.setattr(session, "runtime_arn", lambda role: ARN)
    monkeypatch.setattr(session, "client", lambda region: fake)
    return fake


def test_the_same_name_reuses_its_session_and_a_new_name_gets_a_new_one(tmp_path, monkeypatch, capsys):
    fake = setup(tmp_path, monkeypatch)
    for argv in (["A", "echo hi > /tmp/note"], ["B", "cat /tmp/note"], ["A", "cat /tmp/note"]):
        monkeypatch.setattr(sys, "argv", ["session.py", *argv])
        assert session.main() == 0
    ids = [c["runtimeSessionId"] for c in fake.calls]
    assert ids[0] == ids[2] != ids[1]
    names = json.loads((tmp_path / "sessions-claude-code.json").read_text())
    assert set(names) == {"A", "B"} and len(ids[0]) == 36
    out = capsys.readouterr().out
    assert "microVM up 31 s" in out and out.count("(new: a new microVM)") == 2


def test_the_command_runs_in_a_shell_after_the_uptime_probe(tmp_path, monkeypatch):
    fake = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["session.py", "A", "echo hi > /tmp/note; cat /tmp/note"])
    session.main()
    command = fake.calls[0]["body"]["command"]
    parts = shlex.split(command)
    assert parts[:2] == ["sh", "-c"] and parts[2].endswith("echo hi > /tmp/note; cat /tmp/note")
    assert "/proc/uptime" in parts[2]


def test_stop_ends_every_named_session_and_forgets_them(tmp_path, monkeypatch):
    fake = setup(tmp_path, monkeypatch)
    for name in ("A", "B"):
        monkeypatch.setattr(sys, "argv", ["session.py", name, "true"])
        session.main()
    monkeypatch.setattr(sys, "argv", ["session.py", "--stop"])
    assert session.main() == 0
    assert len(fake.stopped) == 2 and not (tmp_path / "sessions-claude-code.json").exists()
