"""The /ping a coding agent's microVM answers, shared by every role image.

    python3 /app/healthcheck.py --agent claude-code --busy claude

AgentCore Runtime reads /ping to decide whether a session is idle. `Healthy` means
ready for work, so after the Runtime's idle timeout (15 minutes by default) with no
request, the session ends. `HealthyBusy` means working, so the session is kept, up
to its maximum lifetime (8 hours by default). `time_of_last_update` is when the
status last CHANGED, exactly as the AgentCore SDK reports it; a timestamp that moved
on every ping would claim the agent had just changed state.

The agent is busy while its CLI (any process named in --busy) runs in this microVM.
That is the promise of a detached session: Claude Code keeps working with nobody
connected, and once it exits, the idle timeout applies again. A CLI left open at its
prompt therefore keeps the session until you exit it or stop the session, never past
the maximum lifetime.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROC = "/proc"


# Programs that run a CLI's script: for these the script names the process.
INTERPRETERS = {"node", "bun", "deno", "python", "python3", "sh", "bash"}


def _names(pid: str, proc: str = PROC) -> set[str]:
    """What a process is called: its comm and its program's basename; for an interpreter,
    also its script's basename and the npm package the script belongs to
    (`node /usr/local/bin/claude`, `node .../@anthropic-ai/claude-code/cli.js`). Other
    arguments never count, so `tail -f /mnt/s3files/claude/log` is not Claude Code."""
    found: set[str] = set()
    try:
        with open(os.path.join(proc, pid, "comm")) as f:
            found.add(f.read().strip())
        with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
            argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return found
    if argv:
        found.add(os.path.basename(argv[0]))
    if len(argv) > 1 and os.path.basename(argv[0]) in INTERPRETERS:
        script = argv[1]
        found.add(os.path.basename(script))
        parts = script.split("/")
        # A scoped npm package: .../node_modules/@scope/<name>/...
        found.update(parts[i + 1] for i, part in enumerate(parts[:-1])
                     if part.startswith("@") and i + 1 < len(parts))
    return found


def _alive(pid: str, proc: str = PROC) -> bool:
    """A zombie has exited: it keeps its name until it is reaped, but it is not working."""
    try:
        with open(os.path.join(proc, pid, "stat")) as f:
            return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (OSError, IndexError):
        return False


def agent_running(busy: set[str], proc: str = PROC) -> bool:
    me = str(os.getpid())
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit() and p != me]
    except OSError:
        return False
    return any(_names(pid, proc) & busy and _alive(pid, proc) for pid in pids)


def reap() -> None:
    """As PID 1 the server inherits every orphan (a CLI started with nohup whose shell
    exited), so it collects them; otherwise they stay zombies for the session's life."""
    if os.getpid() != 1:
        return
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


class Status:
    """Healthy or HealthyBusy, and when that last changed."""

    def __init__(self, busy: set[str], clock=time.time, probe=agent_running):
        self.busy, self.clock, self.probe = busy, clock, probe
        self.lock = threading.Lock()
        self.status, self.changed = "Healthy", int(clock())

    def read(self) -> tuple[str, int]:
        reap()
        status = "HealthyBusy" if self.busy and self.probe(self.busy) else "Healthy"
        with self.lock:
            if status != self.status:
                self.status, self.changed = status, int(self.clock())
            return self.status, self.changed


def make_handler(agent: str, status: Status):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, body: dict) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_GET(self):
            if self.path == "/ping":
                state, changed = status.read()
                self._json({"status": state, "time_of_last_update": changed,
                            "agent": agent, "storage": "s3files"})
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path == "/invocations":
                self._json({"message": "Use InvokeAgentRuntimeCommandShell "
                                       "(agentcore exec) or InvokeAgentRuntimeCommand"})
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    return Handler


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agent", required=True, help="the role this image serves")
    ap.add_argument("--busy", action="append", default=[],
                    help="a process name that means the agent is working (repeatable)")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args(argv)
    status = Status(set(args.busy))
    print(f"Health check server on :{args.port} ({args.agent}; busy while "
          f"{', '.join(sorted(status.busy)) or 'never'} runs)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(args.agent, status)).serve_forever()


if __name__ == "__main__":
    main()
