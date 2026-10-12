"""fanout.py: the board, the per-agent command, the seed, and the host-side check.

Offline: no session is started. The one real behavior exercised is check_health,
which starts a seeded service on this machine and calls its /health.
"""
from __future__ import annotations

import importlib.util
import json
import shlex
import shutil
import sys
import time
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "coding-agents" / "fanout.py"
spec = importlib.util.spec_from_file_location("workshop_fanout", SOURCE)
fanout = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fanout
spec.loader.exec_module(fanout)


def test_each_agent_reports_its_microvm_age_and_two_isolation_probes():
    inner = shlex.split(fanout.agent_command(3, "/mnt/s3files/fleet/repo-03", "fix {repo}"))[2]
    assert "echo UP $(cut -d. -f1 /proc/uptime)" in inner
    assert "touch /tmp/fanout-agent-03" in inner and "SIBLINGS" in inner
    assert "PEERS $(grep -las" in inner and inner.rstrip().endswith("wait $agent"), \
        "the command's exit status must stay the agent's"
    assert "WORKSHOP_AGENT_WORKDIR=/mnt/s3files/fleet/repo-03" in inner
    assert "boot_id" not in inner, "sessions share a boot ID; it identifies nothing"


def test_the_board_shows_session_age_and_what_each_agent_could_see():
    done = fanout.Tile(1, Path("repo-01"))
    done.up, done.siblings, done.peers, done.state, done.check = "31", "1", "1", "done", "pass"
    done.started, done.finished = time.time() - 90, time.time()
    waiting = fanout.Tile(2, Path("repo-02"))
    board = fanout.render([done, waiting], time.time() - 100, color=False)
    head, _, row1, row2 = board.splitlines()
    assert "2 agents" in head and "1 microVMs up" in head and "1 passed /health" in head
    assert f"session {done.session[:8]}" in row1 and "up   31s" in row1 and "sees 1/1" in row1
    assert "queued" in row2 and "sees ·/·" in row2


@pytest.mark.skipif(not (shutil.which("node") and shutil.which("python3")), reason="needs node and python3")
def test_the_host_check_is_a_real_http_answer(tmp_path):
    repos = fanout.seed(tmp_path, 2)
    assert fanout.check_health(repos[0], timeout_s=5).startswith("fail"), "a seed has no /health yet"
    app = repos[0] / "app.py"
    app.write_text(app.read_text().replace(
        'if self.path == "/items":',
        'if self.path == "/health":\n            body = b\'{"status": "ok"}\'\n'
        '            self.send_response(200)\n            self.end_headers()\n'
        '            self.wfile.write(body)\n        elif self.path == "/items":'))
    assert fanout.check_health(repos[0], timeout_s=10) == "pass"


def test_seeds_differ_in_language_and_start_command(tmp_path):
    repos = fanout.seed(tmp_path, 4)
    starts = {(r / ".start").read_text().split()[0] for r in repos}
    assert starts == {"python", "node"}
    assert all("## Run" in (r / "README.md").read_text() for r in repos)
