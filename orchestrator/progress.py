#!/usr/bin/env python3
"""Where am I in the workshop, and what do I run next? One read-only answer.

    python3 orchestrator/progress.py            # every checkpoint, then NEXT
    python3 orchestrator/progress.py --json     # the same, as JSON

The workshop has three parts: close the lid, twenty at once, and it wakes up. This
walks their end states IN ORDER against real local state and a few cheap, read-only
AWS reads, prints one line per checkpoint, and ends with ONE next step: the page as
the content titles it and the exact command that page teaches. An attendee who fell
behind runs it to catch up; a facilitator asks for its output instead of holding the
room.

It is strictly READ-ONLY, which is what makes it safe to run at any moment and to
paste anywhere. It invokes no model, opens no Runtime session, makes no GitHub call,
and writes no file. It never reads a key, token, password, or private key: the
GitHub App credential is checked with Secrets Manager ``DescribeSecret`` (metadata
only), and ``github-app.env`` is never opened. Account ids are masked. Every check
fails soft to ``?`` on its own, and the whole command is bounded to about fifteen
seconds, so a slow or missing service cannot hang it.

It is a reporting tool, never the verdict path: nothing reads it back, and it may
not import ``llm``, ``reviewer``, or the engine.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# A laptop has no instance metadata service; do not let the SDK spend its default
# retries looking for one. The workshop host answers well inside these limits.
os.environ.setdefault("AWS_METADATA_SERVICE_TIMEOUT", "2")
os.environ.setdefault("AWS_METADATA_SERVICE_NUM_ATTEMPTS", "1")

BUDGET_S = 15.0
DONE, TODO, UNKNOWN = "done", "todo", "unknown"
_MARK = {DONE: "✓", TODO: "✗", UNKNOWN: "?"}

GET_IN = "Get in"
PART1 = "1. Close the lid"
PART2 = "2. Twenty at once"
PART3 = "3. It wakes up"

SECRET_ID = "agentcore/github-mcp/github-app"
GATEWAY_NAME = "github-mcp-gateway"
WEBHOOK_PARAM = "/workshop/webhook-url"
LID = "lid"
FLEET = "fleet"

# Exactly what the pages teach, copied verbatim so a pasted NEXT block runs as-is.
# validate_content.py fails if any line here stops appearing in a page's code block.
CMD_CD = ["cd ~/sample-amazon-bedrock-agentcore-coding-agents/coding-agents"]
CMD_DEPLOY = CMD_CD + ["./deploy-prebuilt.sh claude-code"]
CMD_CONNECT = CMD_CD + ["./connect.sh"]
CMD_START = ["mkdir -p /mnt/s3files/lid && cd /mnt/s3files/lid", "/app/run.sh"]
CMD_RESUME = CMD_CD + ["./connect.sh --resume"]
CMD_OPEN = CMD_CD + ["./open.sh /mnt/s3files/lid"]
CMD_FANOUT = CMD_CD + ["python3 fanout.py --agents 10"]
CMD_GITHUB = CMD_CD + ["./connect-github.sh"]

_ACCOUNT_RE = re.compile(r"(?<![\w])(\d{4})\d{4}(\d{4})(?![\w])")
_RUNTIME_ARN_RE = re.compile(
    r"^arn:aws[a-z-]*:bedrock-agentcore:([a-z0-9-]+):\d{12}:runtime/([A-Za-z0-9_-]+)$")


@dataclass
class Check:
    part: str
    name: str
    state: str = UNKNOWN
    detail: str = ""
    commands: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Spec:
    part: str
    name: str
    run: Callable[[], dict]
    commands: list[str] = field(default_factory=list)
    note: str = ""


# ---------------------------------------------------------------- small helpers
def _mask(text) -> str:
    return _ACCOUNT_RE.sub(r"\1****\2", str(text or ""))


def _repo_root() -> str:
    return os.environ.get("WORKSHOP_REPO_ROOT") or os.path.dirname(_HERE)


def _agents_dir() -> str:
    return os.environ.get("WORKSHOP_CODING_AGENTS_DIR") or os.path.join(_repo_root(), "coding-agents")


def _mount() -> str:
    return os.environ.get("WORKSHOP_S3FILES_DIR", "/mnt/s3files")


def _home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _outcome(state: str, detail: str, **extra) -> dict:
    return {"state": state, "detail": detail, **extra}


def _region() -> str | None:
    arn = _read_json(os.path.join(_agents_dir(), "claude-code", "runtime_config.json")).get("runtime_arn", "")
    match = _RUNTIME_ARN_RE.match(arn or "")
    return match.group(1) if match else (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))


def _client(service: str):
    import boto3  # noqa: PLC0415
    from botocore.config import Config  # noqa: PLC0415
    return boto3.client(service, region_name=_region(), config=Config(
        connect_timeout=3, read_timeout=6, retries={"max_attempts": 1}))


def _error_code(exc: Exception) -> str:
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "") or type(exc).__name__


def _visible_files(path: str) -> list[str]:
    try:
        return sorted(n for n in os.listdir(path) if not n.startswith("."))
    except OSError:
        return []


# ---------------------------------------------------------------- Get in
def check_host() -> dict:
    infra = os.path.join(_agents_dir(), "infra.config")
    if not os.path.isfile(infra):
        return _outcome(TODO, "this host is still being set up (no infra.config yet)")
    if not os.path.ismount(_mount()):
        return _outcome(TODO, f"{_mount()} is not mounted yet")
    return _outcome(DONE, f"shared folder mounted at {_mount()}")


def check_image() -> dict:
    try:
        with open(os.path.join(_agents_dir(), "claude-code", "agent.config"), encoding="utf-8") as handle:
            line = next((l for l in handle if l.startswith("ECR_URI=")), "")
    except OSError:
        line = ""
    uri = line.split("=", 1)[1].strip() if line else ""
    if not uri:
        return _outcome(TODO, "the stack has not finished building the Claude Code image")
    return _outcome(DONE, "ready: " + _mask(uri.rsplit("/", 1)[-1]))


# ---------------------------------------------------------------- 1. Close the lid
def check_runtime() -> dict:
    arn = _read_json(os.path.join(_agents_dir(), "claude-code", "runtime_config.json")).get("runtime_arn", "")
    match = _RUNTIME_ARN_RE.match(arn or "")
    if not match:
        return _outcome(TODO, "no Claude Code Runtime yet")
    try:
        status = _client("bedrock-agentcore-control").get_agent_runtime(
            agentRuntimeId=match.group(2)).get("status", "")
    except ImportError:
        return _outcome(UNKNOWN, f"{match.group(2)} recorded; boto3 is not installed to check it")
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) == "ResourceNotFoundException":
            return _outcome(TODO, f"{match.group(2)} was recorded but no longer exists; deploy again")
        return _outcome(UNKNOWN, f"{match.group(2)} recorded; status unreadable ({_error_code(exc)})")
    if status == "READY":
        return _outcome(DONE, f"{match.group(2)} is READY")
    return _outcome(TODO if status.endswith("FAILED") else UNKNOWN, f"{match.group(2)} is {status}")


def check_detached() -> dict:
    if os.path.isfile(_home(".agentcore", "detached-claude-code")):
        return _outcome(DONE, "you detached from a session; ./connect.sh --resume returns to it")
    return _outcome(TODO, "no detached session recorded yet")


def check_lid_work() -> dict:
    files = _visible_files(os.path.join(_mount(), LID))
    if not files:
        return _outcome(TODO, f"nothing in {_mount()}/{LID} yet")
    shown = ", ".join(files[:4]) + (", ..." if len(files) > 4 else "")
    return _outcome(DONE, f"{len(files)} file(s) in {_mount()}/{LID}: {shown}")


# ---------------------------------------------------------------- 2. Twenty at once
def check_fanout() -> dict:
    summary = _read_json(os.path.join(_mount(), FLEET, "fanout-summary.json"))
    if not summary:
        return _outcome(TODO, "no fan-out run yet")
    agents, passed = summary.get("agents", 0), summary.get("passed_health", 0)
    detail = (f"{agents} agents on {summary.get('microvms', 0)} microVMs, {passed} passed /health, "
              f"{summary.get('wall_s', '?')}s wall clock")
    return _outcome(DONE if agents and passed == agents else TODO, detail)


# ---------------------------------------------------------------- 3. It wakes up
def check_github() -> dict:
    try:
        _client("secretsmanager").describe_secret(SecretId=SECRET_ID)
    except ImportError:
        return _outcome(UNKNOWN, "boto3 is not installed")
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) == "ResourceNotFoundException":
            return _outcome(TODO, "no GitHub App connected yet")
        return _outcome(UNKNOWN, f"credential unreadable ({_error_code(exc)})")
    try:
        control = _client("bedrock-agentcore-control")
        token, found = None, False
        while not found:
            page = control.list_gateways(**({"nextToken": token} if token else {}))
            found = any(item.get("name") == GATEWAY_NAME for item in page.get("items", []))
            token = page.get("nextToken")
            if not token:
                break
    except Exception as exc:  # noqa: BLE001
        return _outcome(UNKNOWN, f"App stored; gateway unreadable ({_error_code(exc)})")
    if not found:
        return _outcome(TODO, "App stored, but its GitHub tools are not deployed yet")
    return _outcome(DONE, "App connected and its GitHub tools deployed")


def check_review() -> dict:
    try:
        url = _client("ssm").get_parameter(Name=WEBHOOK_PARAM)["Parameter"]["Value"]
    except ImportError:
        return _outcome(UNKNOWN, "boto3 is not installed")
    except Exception as exc:  # noqa: BLE001
        return _outcome(UNKNOWN, f"webhook endpoint unreadable ({_error_code(exc)})")
    try:
        logs = _client("logs")
        groups = logs.describe_log_groups(logGroupNamePattern="PrReviewWebhook").get("logGroups", [])
        since = int((time.time() - 2 * 86400) * 1000)
        for group in groups:
            events = logs.filter_log_events(logGroupName=group["logGroupName"], startTime=since,
                                            filterPattern='"REVIEW_POSTED"', limit=20).get("events", [])
            if events:
                last = events[-1]["message"]
                record = json.loads(last[last.index("{"):]) if "{" in last else {}
                return _outcome(DONE, f"an agent reviewed {record.get('repo', '?')}#{record.get('number', '?')} "
                                      f"in {record.get('seconds', '?')}s")
    except Exception as exc:  # noqa: BLE001
        return _outcome(UNKNOWN, f"endpoint ready; reviews unreadable ({_error_code(exc)})")
    host = url.split("/")[2] if url.count("/") >= 2 else url
    return _outcome(TODO, f"endpoint ready ({host.split('.')[0]}...); no review posted yet")


SPECS = [
    Spec(GET_IN, "Workshop host", check_host,
         note="The host is still setting up or the shared folder is missing. Wait a minute and "
              "run progress.py again; if it persists, ask a facilitator."),
    Spec(GET_IN, "Claude Code image", check_image,
         note="The stack builds this image. Wait a minute and run progress.py again."),
    Spec(PART1, "Claude Code Runtime", check_runtime, CMD_DEPLOY),
    Spec(PART1, "Detach and come back", check_detached, CMD_CONNECT + CMD_START,
         note="Connect, start Claude Code in the shared folder, give it the task on the page, "
              "then press Ctrl+] while it works."),
    Spec(PART1, "The work it finished", check_lid_work, CMD_RESUME + CMD_OPEN),
    Spec(PART2, "Twenty at once", check_fanout, CMD_FANOUT),
    Spec(PART3, "GitHub connected", check_github, CMD_GITHUB),
    Spec(PART3, "An agent woke up", check_review,
         note="Open a pull request in your connected repository on GitHub, then watch its "
              "conversation."),
]


def _run_all(specs: list[Spec], budget_s: float = BUDGET_S) -> list[Check]:
    results: dict[int, dict] = {}

    def work(i: int, spec: Spec) -> None:
        try:
            results[i] = spec.run()
        except Exception as exc:  # noqa: BLE001 (one broken check never hides the rest)
            results[i] = _outcome(UNKNOWN, f"check failed: {type(exc).__name__}")

    threads = [threading.Thread(target=work, args=(i, s), daemon=True) for i, s in enumerate(specs)]
    for thread in threads:
        thread.start()
    deadline = time.time() + budget_s
    for thread in threads:
        thread.join(max(0.0, deadline - time.time()))
    checks = []
    for i, spec in enumerate(specs):
        outcome = results.get(i) or _outcome(UNKNOWN, "no answer within the time limit")
        checks.append(Check(spec.part, spec.name, outcome["state"], _mask(outcome["detail"]),
                            list(spec.commands), spec.note))
    return checks


def next_step(checks: list[Check]) -> Check | None:
    """The first checkpoint that is not done, in workshop order."""
    return next((c for c in checks if c.state != DONE), None)


def render(checks: list[Check]) -> str:
    lines, part = [], None
    for check in checks:
        if check.part != part:
            part = check.part
            lines.append(f"\n{part}")
        lines.append(f"  {_MARK[check.state]} {check.name}: {check.detail}")
    step = next_step(checks)
    lines.append("")
    if step is None:
        lines.append("All three parts are done. Next: Take it home, then Go further.")
        return "\n".join(lines)
    lines.append(f"NEXT ({step.part} > {step.name}):")
    if step.note:
        lines.append(f"  {step.note}")
    if step.commands:
        lines.append("")
        lines.extend(f"  {command}" for command in step.commands)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only: where you are, and the next command.")
    parser.add_argument("--json", action="store_true", help="print the checkpoints as JSON")
    args = parser.parse_args(argv)
    checks = _run_all(SPECS)
    if args.json:
        step = next_step(checks)
        print(json.dumps({"checks": [asdict(c) for c in checks],
                          "next": asdict(step) if step else None}, indent=2))
    else:
        print(render(checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
