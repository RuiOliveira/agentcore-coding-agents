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
import threading
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
    assert fanout.check_health(repos[0], timeout_s=5)[0].startswith("fail"), "a seed has no /health yet"
    app = repos[0] / "app.py"
    app.write_text(app.read_text().replace(
        'if self.path == "/items":',
        'if self.path == "/health":\n            body = b\'{"status": "ok", "version": "1.0"}\'\n'
        '            self.send_response(200)\n            self.end_headers()\n'
        '            self.wfile.write(body)\n        elif self.path == "/items":'))
    # The standard you edited shows up as served, on the board, for every service.
    assert fanout.check_health(repos[0], timeout_s=10) == ("pass", "version=1.0")


def test_a_job_in_its_own_folder_gets_an_index_of_every_agent(tmp_path):
    tiles = [fanout.Tile(1, tmp_path / "repo-01", state="done", last="DONE a boss fish <b>"),
             fanout.Tile(2, tmp_path / "repo-02", state="failed", error="exit 1")]
    page = fanout.write_index(tmp_path, tiles).read_text()
    assert 'href="repo-01/"' in page and "a boss fish &lt;b&gt;" in page
    assert 'class="card failed"' in page and "exit 1" in page


def test_seeds_differ_in_language_and_start_command(tmp_path):
    repos = fanout.seed(tmp_path, 4)
    starts = {(r / ".start").read_text().split()[0] for r in repos}
    assert starts == {"python", "node"}
    assert all("## Run" in (r / "README.md").read_text() for r in repos)


def test_an_index_someone_else_wrote_is_left_alone(tmp_path):
    tiles = [fanout.Tile(1, tmp_path / "repo-01", state="done", last="DONE x")]
    mine = tmp_path / "index.html"
    mine.write_text("<h1>my own arcade page</h1>")
    assert fanout.write_index(tmp_path, tiles) is None
    assert mine.read_text() == "<h1>my own arcade page</h1>"
    mine.unlink()
    assert fanout.write_index(tmp_path, tiles) == mine, "an absent page is written"
    assert fanout.write_index(tmp_path, tiles) == mine, "fanout may rewrite its own page"
    assert mine.read_text().startswith(fanout.INDEX_MARK)


def test_the_health_summary_names_fields_and_how_many_serve_them():
    tiles = []
    for i, health in enumerate(["service=a-01 version=1.0", "service=b-02 version=1.0", "service=c-03"]):
        tiles.append(fanout.Tile(i + 1, Path(f"repo-{i + 1:02d}"), check="pass", health=health))
    tiles.append(fanout.Tile(4, Path("repo-04"), check="fail: no /health"))
    assert fanout.health_fields(tiles) == "service on 3/3, version on 2/3"


def test_a_session_that_failed_before_any_work_gets_one_new_session():
    calls = []

    def flaky(tile, *args):
        calls.append(tile.session)
        if len(calls) == 1:
            tile.state, tile.error = "failed", "RuntimeClientError: x"
            tile.errors.append("RuntimeClientError: x")
        else:
            tile.state, tile.up = "done", "25"

    tile = fanout.Tile(1, Path("repo-01"))
    fanout.run_tile_with_one_retry(tile, "arn", "us-east-1", "/mnt/s3files/fleet/repo-01", "t",
                                   threading.Lock(), run=flaky)
    assert tile.state == "done" and tile.retried and len(set(calls)) == 2
    assert tile.errors, "the first failure is kept for the summary"


def test_a_failure_after_work_began_is_a_result_not_a_retry():
    calls = []

    def fails_late(tile, *args):
        calls.append(1)
        tile.up, tile.state, tile.error = "25", "failed", "exit 1"
        tile.errors.append("exit 1")

    tile = fanout.Tile(1, Path("repo-01"))
    fanout.run_tile_with_one_retry(tile, "arn", "us-east-1", "/x", "t", threading.Lock(), run=fails_late)
    assert len(calls) == 1 and tile.state == "failed" and not tile.retried
