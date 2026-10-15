#!/usr/bin/env python3
"""Fan one job out to many Claude Code sessions at once, and watch them on one board.

    python3 fanout.py                  # 10 agents, the consolidation job below
    python3 fanout.py --agents 20      # twenty microVMs, same command
    python3 fanout.py --task "..."     # your own job; {repo} is the folder, {n} the agent number

Every agent is a NEW session on the same Claude Code Runtime, so every agent is its
own microVM: its own memory, processes, and /tmp. They share exactly one thing on
purpose, the S3 Files mount, where each one owns a single folder. The board shows
each agent's session ID, how long its microVM had been up when the agent started,
two isolation probes, and whether its change passed a real check run here on the
host.

The probes count what one agent can see of the others. Each agent leaves a file in
its /tmp and counts the agent files there, and counts the Claude Code processes it
can see. On one shared machine the last agent would count all of them; in separate
microVMs every count is 1. (The kernel's boot ID is no evidence either way: sessions
start from the same prepared image, and a live check on 2026-10-12 found the same
boot ID and MAC addresses in two isolated sessions.)

The default job is the consolidation the abstract promises: N small services that
grew up apart, each in its own style, and one standard to apply everywhere (a
/health endpoint and a README note). One agent per repository, all at once.

This is a reporting and orchestration tool for the workshop; it never grades code by
reading it. The host starts each changed service and calls /health, and that real
response is the check.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOUNT = Path(os.environ.get("WORKSHOP_S3FILES_DIR", "/mnt/s3files"))
FLEET = "fleet"

TASK = (
    "You are one of many agents working at the same time; this folder is yours alone: {repo}. "
    "It holds one small HTTP service. Bring it to our team standard, which the CLAUDE.md "
    "in the fleet folder above it describes, in the language and style the service "
    "already uses, without new dependencies and without changing any other behavior. "
    "Work only inside {repo}. When you are done, print exactly one final line: "
    "DONE <one sentence on what you changed>."
)

# The platform team's standard, as a file every agent reads: Claude Code loads CLAUDE.md
# from its working folder and every parent. Seeded once; your edits are never replaced.
STANDARD = """# Our service standard

Every service in this fleet must have:

- A `GET /health` endpoint that returns HTTP 200 with the JSON body
  `{"status": "ok", "service": "<the service name from README.md>"}`.
- A short "Health check" section in README.md with a curl example.
"""

# Small services that grew up apart: different languages, styles, ports, and layouts.
SEEDS = [
    ("python", "inventory", "app.py", '''import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import os

ITEMS = [{"sku": "A-1", "qty": 4}, {"sku": "B-2", "qty": 0}]

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/items":
            body = json.dumps(ITEMS).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
'''),
    ("node", "billing", "server.js", '''const http = require("http");
const port = Number(process.env.PORT || 3000);

const invoices = [{ id: 7, total: 42.5 }];

http.createServer((req, res) => {
  if (req.url === "/invoices") {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify(invoices));
  }
  res.writeHead(404);
  res.end();
}).listen(port);
'''),
    ("python", "search", "service.py", '''"""Search service: GET /search?q=term"""
import json, os, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DOCS = ["agentcore runtime", "microvm isolation", "s3 files"]


class SearchHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != "/search":
            self.send_error(404)
            return
        q = urllib.parse.parse_qs(url.query).get("q", [""])[0]
        hits = [d for d in DOCS if q in d]
        payload = json.dumps({"q": q, "hits": hits}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)


ThreadingHTTPServer(("", int(os.environ["PORT"]) if "PORT" in os.environ else 9000), SearchHandler).serve_forever()
'''),
    ("node", "notifications", "index.js", '''// notifications: POST /send, GET /sent
const { createServer } = require("node:http");

const sent = [];

function json(res, status, value) {
  res.statusCode = status;
  res.setHeader("content-type", "application/json");
  res.end(JSON.stringify(value));
}

const server = createServer((req, res) => {
  if (req.method === "GET" && req.url === "/sent") return json(res, 200, sent);
  if (req.method === "POST" && req.url === "/send") {
    sent.push({ at: Date.now() });
    return json(res, 202, { queued: true });
  }
  json(res, 404, { error: "not found" });
});

server.listen(process.env.PORT ?? 4000);
'''),
]


def seed(root: Path, n: int) -> list[Path]:
    """Create N small repositories under root, cycling the styles above."""
    root.mkdir(parents=True, exist_ok=True)
    standard = root / "CLAUDE.md"
    if not standard.exists():
        standard.write_text(STANDARD)
    repos = []
    for i in range(1, n + 1):
        lang, name, entry, code = SEEDS[(i - 1) % len(SEEDS)]
        service = f"{name}-{i:02d}"
        repo = root / f"repo-{i:02d}"
        if repo.exists():
            shutil.rmtree(repo)
        repo.mkdir()
        (repo / entry).write_text(code)
        start = f"PORT=8080 python3 {entry}" if lang == "python" else f"PORT=8080 node {entry}"
        (repo / "README.md").write_text(
            f"# {service}\n\nA small {lang} service.\n\n## Run\n\n```bash\n{start}\n```\n")
        (repo / ".start").write_text(f"{lang} {entry}\n")
        repos.append(repo)
    return repos


def runtime_arn(role: str = "claude-code") -> str:
    """$ARN if set, else a recorded deploy, else the Runtime named after the role."""
    if os.environ.get("ARN", "").startswith("arn:"):
        return os.environ["ARN"]
    config = HERE / role / "runtime_config.json"
    if config.exists():
        return json.loads(config.read_text())["runtime_arn"]
    import boto3  # noqa: PLC0415
    name = role.replace("-", "_")
    pages = boto3.client("bedrock-agentcore-control").get_paginator("list_agent_runtimes")
    for page in pages.paginate():
        for runtime in page.get("agentRuntimes", []):
            if runtime.get("agentRuntimeName") == name:
                return runtime["agentRuntimeArn"]
    sys.exit(f"No Runtime named {name} yet. Create it first (Lab 1, Create the Runtime and Open a Session, step 1).")


def agent_command(index: int, repo_in_vm: str, task: str) -> str:
    """The command one session runs: report its microVM's age, probe isolation, then work.

    UP is the microVM's uptime when the agent starts. SIBLINGS counts agent files in
    /tmp after this agent writes its own. PEERS counts the Claude Code processes this
    agent can see once its own is running. Both are 1 in an isolated microVM.
    """
    prompt = task.format(repo=repo_in_vm, n=index)
    probe = (
        "echo UP $(cut -d. -f1 /proc/uptime); "
        f"touch /tmp/fanout-agent-{index:02d}; echo SIBLINGS $(ls /tmp/fanout-agent-* | wc -l); "
    )
    work = (f"WORKSHOP_AGENT_WORKDIR={shlex.quote(repo_in_vm)} "
            f"/app/run.sh --print {shlex.quote(prompt)} & agent=$!; "
            "sleep 8; echo PEERS $(grep -las '[-]-dangerously-skip-permissions' /proc/[0-9]*/cmdline | wc -l); "
            "wait $agent")
    return "sh -c " + shlex.quote(probe + work)


@dataclass
class Tile:
    index: int
    repo: Path
    session: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: str = "queued"
    up: str = ""
    siblings: str = ""
    peers: str = ""
    last: str = ""
    started: float = 0.0
    finished: float = 0.0
    check: str = ""
    health: str = ""  # what /health returned beyond its status, e.g. version=1.0
    error: str = ""
    errors: list = field(default_factory=list)  # every failure, whole, for the summary
    retried: bool = False

    def elapsed(self, now: float) -> float:
        if not self.started:
            return 0.0
        return (self.finished or now) - self.started


def run_tile(tile: Tile, arn: str, region: str, repo_in_vm: str, task: str, lock: threading.Lock) -> None:
    import boto3
    from botocore.config import Config
    client = boto3.client("bedrock-agentcore", region_name=region,
                          config=Config(read_timeout=900, retries={"max_attempts": 2}))
    tile.started, tile.state = time.time(), "starting"
    try:
        resp = client.invoke_agent_runtime_command(
            agentRuntimeArn=arn, runtimeSessionId=tile.session,
            body={"command": agent_command(tile.index, repo_in_vm, task), "timeout": 900})
        buffer, code = "", None
        for event in resp["stream"]:
            chunk = event.get("chunk") or {}
            delta = chunk.get("contentDelta") or {}
            text = (delta.get("stdout") or "") + (delta.get("stderr") or "")
            if text:
                buffer += text
                *lines, buffer = buffer.split("\n")
                for line in lines:
                    line = line.strip()
                    with lock:
                        if line.startswith("UP "):
                            tile.up, tile.state = line.split()[-1], "working"
                        elif line.startswith("SIBLINGS "):
                            tile.siblings = line.split()[-1]
                        elif line.startswith("PEERS "):
                            tile.peers = line.split()[-1]
                        elif line:
                            tile.last = line[:120]
            if "contentStop" in chunk:
                code = chunk["contentStop"].get("exitCode")
        if buffer.strip():
            tile.last = buffer.strip()[:120]
        tile.state = "done" if code == 0 else "failed"
        if code != 0:
            tile.error = f"exit {code}"
    except Exception as exc:  # noqa: BLE001 (shown on the board, never hidden)
        tile.state, tile.error = "failed", f"{type(exc).__name__}: {exc}"[:120]
        tile.errors.append(f"session {tile.session}: {type(exc).__name__}: {exc}"[:600])
    finally:
        tile.finished = time.time()
        try:
            client.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=tile.session)
        except Exception:  # noqa: BLE001 (the session may already have ended)
            pass


def run_tile_with_one_retry(tile: Tile, arn: str, region: str, repo_in_vm: str, task: str,
                            lock: threading.Lock, run=None) -> None:
    """A session that failed before its agent reported anything did no work, so it gets
    one more try in a NEW session (a new microVM). A failure after work began is a
    result: it stays on the board, never retried."""
    run = run or run_tile
    run(tile, arn, region, repo_in_vm, task, lock)
    if tile.state == "failed" and not tile.up and tile.errors and not tile.retried:
        with lock:
            tile.retried, tile.session = True, str(uuid.uuid4())
            tile.state, tile.error, tile.last = "starting", "", ""
        run(tile, arn, region, repo_in_vm, task, lock)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _extra(body: dict) -> str:
    """The fields a service's /health returns beyond `status`: your standard, as served."""
    return " ".join(f"{k}={v}" for k, v in sorted(body.items()) if k != "status")[:40]


def check_health(repo: Path, timeout_s: float = 15.0) -> tuple[str, str]:
    """Start the changed service on the host and call /health. The response decides."""
    import urllib.request
    lang, entry = (repo / ".start").read_text().split()
    port = free_port()
    cmd = ["python3", entry] if lang == "python" else ["node", entry]
    proc = subprocess.Popen(cmd, cwd=repo, env={**os.environ, "PORT": str(port)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        end = time.time() + timeout_s
        while time.time() < end:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    body = json.loads(r.read().decode() or "{}")
                    if r.status == 200 and isinstance(body, dict) and body.get("status") == "ok":
                        return "pass", _extra(body)
                    return f"fail: {body}"[:60], ""
            except Exception:  # noqa: BLE001 (not up yet, or no /health)
                time.sleep(0.4)
        return "fail: no /health", ""
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


COLORS = {"queued": "90", "starting": "33", "working": "36", "done": "32", "failed": "31"}
DOTS = {"queued": "·", "starting": "◌", "working": "●", "done": "✓", "failed": "✗"}


def render(tiles: list[Tile], started: float, color: bool = True) -> str:
    now = time.time()
    vms = sum(bool(t.up) for t in tiles)
    done = sum(t.state == "done" for t in tiles)
    passed = sum(t.check == "pass" for t in tiles)
    head = (f"  {len(tiles)} agents  ·  {vms} microVMs up  ·  {done} finished  ·  "
            f"{passed} passed /health  ·  {now - started:5.0f}s")
    rows = [head, "  " + "─" * (len(head) - 2)]
    for t in tiles:
        dot = DOTS[t.state]
        seen = f"{t.siblings or '·'}/{t.peers or '·'}"
        line = (f"  {dot} {t.repo.name:8}  session {t.session[:8]}  up {(t.up + 's') if t.up else '·':>5}  "
                f"sees {seen:3}  {t.state:8} {t.elapsed(now):5.0f}s  {(t.check or '').ljust(6)}  "
                f"{(t.health + '  ') if t.health else ''}{'(second session) ' if t.retried else ''}"
                f"{t.error or t.last}")[:160]
        rows.append(f"\033[{COLORS[t.state]}m{line}\033[0m" if color else line)
    return "\n".join(rows)


# The first line of every index page fanout writes; a page without it is someone else's.
INDEX_MARK = "<!-- written by coding-agents/fanout.py; rerunning the job rewrites it -->\n"


def health_fields(tiles: list[Tile]) -> str:
    """Which fields the passing services' /health served beyond status, and on how many:
    the standard you wrote, as the fleet actually serves it."""
    passing = [t for t in tiles if t.check == "pass"]
    counts: dict[str, int] = {}
    for t in passing:
        for key in {pair.split("=", 1)[0] for pair in t.health.split() if "=" in pair}:
            counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{k} on {n}/{len(passing)}" for k, n in sorted(counts.items()))


def write_index(root: Path, tiles: list[Tile]) -> Path | None:
    """An index of every agent's folder with its final line, so a folder of games is an
    arcade. None when the folder already has an index.html that fanout did not write."""
    import html
    cards = []
    for t in tiles:
        name = html.escape(t.repo.name)
        note = html.escape((t.last or t.error or t.state).removeprefix("DONE ").strip())
        cards.append(f'<a class="card {t.state}" href="{name}/"><b>{name}</b><span>{note}</span></a>')
    page = ("<!doctype html><meta charset=utf-8><title>Arcade</title><style>"
            "body{font:16px system-ui;background:#0b1020;color:#e8ecff;margin:2rem}"
            "main{display:grid;grid-template-columns:repeat(auto-fill,minmax(14rem,1fr));gap:1rem}"
            ".card{display:flex;flex-direction:column;gap:.4rem;padding:1rem;border-radius:.6rem;"
            "background:#18214a;color:inherit;text-decoration:none}.card:hover{background:#26337a}"
            ".failed{opacity:.5}span{font-size:.9rem;color:#b8c2ff}</style>"
            f"<h1>Arcade: {len(tiles)} microVMs, one twist each</h1><main>{''.join(cards)}</main>\n")
    target = root / "index.html"
    if target.exists() and INDEX_MARK not in target.read_text(errors="replace"):
        return None
    target.write_text(INDEX_MARK + page)
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agents", type=int, default=10, help="how many sessions (microVMs) to start")
    ap.add_argument("--task", default=TASK, help="the job; {repo} is each agent's folder, {n} its number")
    ap.add_argument("--no-seed", action="store_true", help="keep the existing repositories")
    ap.add_argument("--no-check", action="store_true", help="skip the host-side /health check")
    ap.add_argument("--folder", default=FLEET,
                    help="folder under the mount; a new one gets an empty repo-NN per agent")
    a = ap.parse_args()
    if not 1 <= a.agents <= 50:
        ap.error("--agents must be between 1 and 50")
    if not MOUNT.is_dir():
        sys.exit(f"{MOUNT} is not mounted on this host")
    arn = runtime_arn()
    region = arn.split(":")[3]
    root = MOUNT / a.folder
    if a.folder != FLEET:
        # Your own job in its own folder: one empty folder per agent, nothing seeded.
        a.no_seed = a.no_check = True
        for i in range(1, a.agents + 1):
            (root / f"repo-{i:02d}").mkdir(parents=True, exist_ok=True)
    repos = sorted(root.glob("repo-*"))[: a.agents] if a.no_seed else seed(root, a.agents)
    if len(repos) < a.agents:
        sys.exit(f"Only {len(repos)} repositories under {root}; drop --no-seed to create them")
    tiles = [Tile(i + 1, repo) for i, repo in enumerate(repos)]
    lock = threading.Lock()
    started = time.time()
    threads = [threading.Thread(target=run_tile_with_one_retry, daemon=True,
                                args=(t, arn, region, f"/mnt/s3files/{a.folder}/{t.repo.name}", a.task, lock))
               for t in tiles]
    for th in threads:
        th.start()
    tty = sys.stdout.isatty()
    lines = 0
    while True:
        alive = any(th.is_alive() for th in threads)
        if not alive and not a.no_check:
            for t in tiles:
                if t.state == "done" and not t.check:
                    t.check, t.health = check_health(t.repo)
        frame = render(tiles, started, color=tty)
        if tty:
            sys.stdout.write(("\033[F" * lines) + "\033[J" + frame + "\n")
            lines = frame.count("\n") + 1
        sys.stdout.flush()
        if not alive:
            break
        time.sleep(0.5)
    if not tty:
        print(frame)
    wall = time.time() - started
    serial = sum(t.elapsed(time.time()) for t in tiles)
    print(f"\n  Wall clock {wall:.0f}s for {len(tiles)} agents. One after another, the same work "
          f"would have taken about {serial / 60:.0f} min.")
    counts = sorted({t.siblings for t in tiles if t.siblings})
    peers = sorted({t.peers for t in tiles if t.peers})
    print(f"  {sum(bool(t.up) for t in tiles)} sessions, each its own microVM. What each agent could see "
          f"of the others: agent files in /tmp {', '.join(counts) or '?'}; Claude Code processes "
          f"{', '.join(peers) or '?'} (1 means only its own).")
    for t in tiles:
        for err in t.errors:
            print(f"  {t.repo.name}: {err}")
    fields = health_fields(tiles)
    if fields:
        print(f"  /health beyond status: {fields}")
    print(f"  The changes are on the shared mount: {root}/repo-*")
    if a.folder != FLEET:
        if write_index(root, tiles):
            print(f"  One page for all of them: {root}/index.html")
        else:
            print(f"  Left {root}/index.html alone: fanout did not write it.")
    # A record of this run for progress.py; it describes the run and judges nothing.
    summary = {"at": int(time.time()), "agents": len(tiles),
               "finished": sum(t.state == "done" for t in tiles),
               "passed_health": sum(t.check == "pass" for t in tiles),
               "microvms": sum(bool(t.up) for t in tiles),
               "sibling_counts": counts, "peer_counts": peers, "wall_s": round(wall), "serial_s": round(serial),
               "retried": sum(t.retried for t in tiles),
               "errors": {t.repo.name: t.errors for t in tiles if t.errors}}
    try:
        (root / "fanout-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    except OSError:
        pass
    return 0 if all(t.state == "done" for t in tiles) else 1


if __name__ == "__main__":
    sys.exit(main())
