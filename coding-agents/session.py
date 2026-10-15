#!/usr/bin/env python3
"""Run one shell command in a named Runtime session: the API fanout.py loops over.

    python3 session.py A 'echo hello > /tmp/note; cat /tmp/note'   # session A: a new microVM
    python3 session.py B 'cat /tmp/note'                           # session B: another microVM
    python3 session.py A 'cat /tmp/note'                           # back in A: the note is there
    python3 session.py --stop                                      # end every named session

A name maps to one runtimeSessionId, kept in ~/.agentcore/sessions-<role>.json, and a
session ID is what AgentCore Runtime routes to a microVM: the first command for a new
ID starts one, and every later command with that ID runs in the same one. That is the
whole isolation model, and this script shows it with one call:

    bedrock-agentcore.invoke_agent_runtime_command(
        agentRuntimeArn=..., runtimeSessionId=<the ID for this name>,
        body={"command": "sh -c '<your command>'", "timeout": 120})

Each run prints which session it used and how long its microVM has been up, so a new
microVM (seconds) is easy to tell from a reused one (minutes).
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".agentcore"


def runtime_arn(role: str) -> str:
    config = HERE / role / "runtime_config.json"
    if not config.exists():
        sys.exit(f"No {role} Runtime yet. Create it first:  ./deploy-prebuilt.sh {role}")
    return json.loads(config.read_text())["runtime_arn"]


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def client(region: str):
    import boto3
    from botocore.config import Config
    return boto3.client("bedrock-agentcore", region_name=region,
                        config=Config(read_timeout=300, retries={"max_attempts": 2}))


def run(arn: str, session: str, command: str, timeout: int) -> int:
    probe = "printf 'UP %s\\n' \"$(cut -d. -f1 /proc/uptime)\"; "
    resp = client(arn.split(":")[3]).invoke_agent_runtime_command(
        agentRuntimeArn=arn, runtimeSessionId=session,
        body={"command": "sh -c " + shlex.quote(probe + command), "timeout": timeout})
    code, first = None, True
    for event in resp["stream"]:
        chunk = event.get("chunk") or {}
        delta = chunk.get("contentDelta") or {}
        out = delta.get("stdout") or ""
        if first and out.startswith("UP "):
            line, _, out = out.partition("\n")
            print(f"  microVM up {line.split()[1]} s")
            first = False
        if out:
            sys.stdout.write(out)
        if delta.get("stderr"):
            sys.stderr.write(delta["stderr"])
        if "contentStop" in chunk:
            code = chunk["contentStop"].get("exitCode")
    sys.stdout.flush()
    return code if isinstance(code, int) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?", help="a session name you choose, such as A or B")
    ap.add_argument("command", nargs="?", help="a shell command to run in that session")
    ap.add_argument("--role", default="claude-code", help="which deployed agent's Runtime to use")
    ap.add_argument("--timeout", type=int, default=120, help="seconds the command may run")
    ap.add_argument("--stop", action="store_true", help="stop every named session and forget them")
    a = ap.parse_args()
    arn = runtime_arn(a.role)
    state = STATE_DIR / f"sessions-{a.role}.json"
    names = load(state)
    if a.stop:
        c = client(arn.split(":")[3])
        for name, session in names.items():
            try:
                c.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=session)
                print(f"stopped {name} ({session})")
            except Exception:  # noqa: BLE001 (already ended)
                print(f"{name} ({session}) had already ended")
        state.unlink(missing_ok=True)
        return 0
    if not a.name or a.command is None:
        ap.error("give a session name and a command, or --stop")
    new = a.name not in names
    session = names.setdefault(a.name, str(uuid.uuid4()))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(names, indent=2) + "\n")
    print(f"session {a.name} = {session}{' (new: a new microVM)' if new else ''}", flush=True)
    return run(arn, session, a.command, a.timeout)


if __name__ == "__main__":
    sys.exit(main())
