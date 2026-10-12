"""Wake a Claude Code session on AgentCore Runtime when a pull request opens.

GitHub calls this function's HTTP endpoint for every pull request event of the
workshop App. The function checks GitHub's signature, answers within a second, and
hands the work to an asynchronous copy of itself. That copy:

1. finds the attendee's Claude Code Runtime (``claude_code``) and the GitHub Gateway,
2. posts "an agent woke up" on the pull request,
3. reads the pull request's diff through the Gateway,
4. starts a NEW Runtime session (its own microVM) that reviews the diff with Claude Code,
5. posts that review back to the pull request through the Gateway.

The GitHub credential never enters the Runtime: the Gateway's GitHub tools hold it,
and the session only receives the diff text. The author of a change is never its
reviewer, because the review runs in a fresh session that wrote none of the code.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shlex
import time
import urllib.error
import urllib.request
import uuid

SECRET_ID = "agentcore/github-mcp/github-app"
GATEWAY_NAME = "github-mcp-gateway"
RUNTIME_NAME = "claude_code"
TARGET = "GitHubMCP"
MAX_DIFF_BYTES = 40_000  # base64 grows it by a third; one command allows 65,536 characters
ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}
BEGIN, END = "===REVIEW-BEGIN===", "===REVIEW-END==="

PROMPT = (
    "You are a careful code reviewer, working alone in this microVM. The file "
    "/tmp/review/pr.diff holds pull request #{number} \"{title}\" by {author} in {repo}. "
    "Each added or unchanged line starts with its line number in the new file. Read it, "
    "then write the review in GitHub Markdown: a one-sentence summary; then at most five "
    "findings, each a definite problem you can show from the diff, naming the file and "
    "line, why it matters, and a concrete fix, in one or two sentences; then a final line "
    "that is exactly 'Verdict: looks good' or 'Verdict: needs changes'. Skip anything you "
    "are unsure of and do not restate the diff. Print only the review."
)
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_session = None


def _boto():
    global _session
    if _session is None:
        import boto3
        _session = boto3.session.Session()
    return _session


def region() -> str:
    return os.environ.get("AWS_REGION", "us-west-2")


def response(status: int, message: str) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json"},
            "body": json.dumps({"message": message})}


def webhook_secret() -> str:
    """The secret GitHub signs deliveries with, stored by deploy-credential.sh."""
    sm = _boto().client("secretsmanager", region_name=region())
    try:
        value = json.loads(sm.get_secret_value(SecretId=SECRET_ID)["SecretString"])
    except sm.exceptions.ResourceNotFoundException:
        return ""
    return value.get("webhook_secret") or ""


def signature_ok(secret: str, body: bytes, header: str) -> bool:
    if not secret or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def job_from_event(kind: str, payload: dict) -> dict | None:
    """The review job for a pull request event, or None when it needs no review."""
    if kind != "pull_request" or payload.get("action") not in ACTIONS:
        return None
    pr = payload.get("pull_request") or {}
    if pr.get("draft") or pr.get("state") != "open":
        return None
    repo = payload["repository"]
    return {"owner": repo["owner"]["login"], "repo": repo["name"], "number": pr["number"],
            "title": pr.get("title", ""), "author": (pr.get("user") or {}).get("login", ""),
            "sha": pr["head"]["sha"]}


def handler(event, context):
    if "review" in event:
        return review(event["review"])
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    secret = webhook_secret()
    if not secret:
        return response(503, "No webhook secret yet: connect the repository first.")
    if not signature_ok(secret, raw, headers.get("x-hub-signature-256", "")):
        return response(401, "Signature does not match this App's webhook secret.")
    kind = headers.get("x-github-event", "")
    if kind == "ping":
        return response(200, "pong")
    job = job_from_event(kind, json.loads(raw or b"{}"))
    if job is None:
        return response(202, f"Ignored {kind} event.")
    job["received_at"] = time.time()
    _boto().client("lambda", region_name=region()).invoke(
        FunctionName=context.invoked_function_arn, InvocationType="Event",
        Payload=json.dumps({"review": job}).encode())
    return response(202, "An agent is waking up to review this pull request.")


# ---------------- Gateway (SigV4-signed MCP JSON-RPC) ----------------

def gateway_url() -> str:
    control = _boto().client("bedrock-agentcore-control", region_name=region())
    token = None
    while True:
        page = control.list_gateways(**({"nextToken": token} if token else {}))
        for item in page.get("items", []):
            if item.get("name") == GATEWAY_NAME:
                return control.get_gateway(gatewayIdentifier=item["gatewayId"])["gatewayUrl"]
        token = page.get("nextToken")
        if not token:
            raise RuntimeError(f"Gateway {GATEWAY_NAME} not found; run the connect step first")


def tool(url: str, name: str, arguments: dict):
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": f"{TARGET}___{name}", "arguments": arguments}}).encode()
    creds = _boto().get_credentials().get_frozen_credentials()
    request = AWSRequest(method="POST", url=url, data=body,
                         headers={"Content-Type": "application/json",
                                  "Accept": "application/json, text/event-stream"})
    SigV4Auth(creds, "bedrock-agentcore", region()).add_auth(request)
    req = urllib.request.Request(url, data=body, headers=dict(request.headers), method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.loads(r.read().decode() or "{}")
    if "error" in payload:
        raise RuntimeError(f"{name}: {payload['error']}")
    for block in (payload.get("result") or {}).get("content", []):
        if block.get("type") == "text":
            text = block.get("text", "")
            if text.lower().startswith(("error calling tool", "error executing tool")):
                raise RuntimeError(f"{name}: {text[:300]}")
            try:
                return json.loads(text)
            except ValueError:
                return text
    return payload.get("result")


# ---------------- The review session ----------------

def runtime_arn() -> str:
    control = _boto().client("bedrock-agentcore-control", region_name=region())
    token = None
    while True:
        page = control.list_agent_runtimes(**({"nextToken": token} if token else {}))
        for item in page.get("agentRuntimes", []):
            if item.get("agentRuntimeName") == RUNTIME_NAME:
                return item["agentRuntimeArn"]
        token = page.get("nextToken")
        if not token:
            raise RuntimeError(f"Runtime {RUNTIME_NAME} not found; deploy Claude Code first")


def number_patch(patch: str) -> str:
    """Prefix each added or unchanged line with its line number in the new file."""
    out, line_no = [], None
    for line in patch.splitlines():
        hunk = HUNK.match(line)
        if hunk:
            line_no = int(hunk.group(1))
            out.append(line)
        elif line_no is None or line.startswith("\\"):
            out.append(line)
        elif line.startswith("-"):
            out.append(f"     {line}")
        else:
            out.append(f"{line_no:4d} {line}")
            line_no += 1
    return "\n".join(out)


def build_diff(pr: dict) -> str:
    parts = []
    for f in pr.get("files", []):
        header = f"diff --git a/{f['filename']} b/{f['filename']}  ({f['status']}, +{f['additions']} -{f['deletions']})"
        patch = f.get("patch")
        parts.append(header + "\n" + (number_patch(patch) if patch else "(no patch: binary or too large)"))
    diff = "\n\n".join(parts)
    data = diff.encode()
    if len(data) > MAX_DIFF_BYTES:
        diff = data[:MAX_DIFF_BYTES].decode(errors="ignore") + "\n\n[diff truncated: only the first 40 KB was reviewed]"
    return diff


def build_command(job: dict, diff: str) -> str:
    prompt = PROMPT.format(number=job["number"], title=job["title"].replace('"', "'"),
                           author=job["author"], repo=f"{job['owner']}/{job['repo']}")
    encoded = base64.b64encode(diff.encode()).decode()
    script = (
        "mkdir -p /tmp/review && cd /tmp/review && "
        f"echo {encoded} | base64 -d > pr.diff && "
        f"echo {BEGIN} && WORKSHOP_AGENT_WORKDIR=/tmp/review /app/run.sh --print {shlex.quote(prompt)}; "
        f"echo {END}"
    )
    return "sh -c " + shlex.quote(script)


def extract_review(stdout: str) -> str:
    if BEGIN not in stdout:
        return ""
    return stdout.split(BEGIN, 1)[1].split(END, 1)[0].strip()


def run_session(arn: str, session: str, command: str) -> tuple[str, int | None]:
    from botocore.config import Config
    client = _boto().client("bedrock-agentcore", region_name=region(),
                            config=Config(read_timeout=840, retries={"max_attempts": 1}))
    resp = client.invoke_agent_runtime_command(
        agentRuntimeArn=arn, runtimeSessionId=session,
        body={"command": command, "timeout": 600})
    out, code = [], None
    for event in resp["stream"]:
        chunk = event.get("chunk") or {}
        if "contentDelta" in chunk:
            out.append(chunk["contentDelta"].get("stdout") or "")
        if "contentStop" in chunk:
            code = chunk["contentStop"].get("exitCode")
    try:
        client.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=session)
    except Exception:  # noqa: BLE001 (the session may already have ended)
        pass
    return "".join(out), code


def review(job: dict) -> dict:
    started = time.time()
    url = gateway_url()
    where = {"owner": job["owner"], "repo": job["repo"], "issue_number": job["number"]}
    session = f"pr-review-{job['number']}-{uuid.uuid4()}"
    tool(url, "comment_on_issue", {**where, "body": (
        "**An agent just woke up** to review this pull request, in its own microVM "
        f"(session `{session[-12:]}`). Nobody started it by hand: the pull request did. "
        "Its review will appear below in a minute or two.")})
    pr = tool(url, "get_pull_request_files",
              {"owner": job["owner"], "repo": job["repo"], "number": job["number"]})
    stdout, code = run_session(runtime_arn(), session, build_command(job, build_diff(pr)))
    text = extract_review(stdout)
    seconds = time.time() - job.get("received_at", started)
    if code == 0 and text:
        body = (f"### Review from Claude Code on Amazon Bedrock AgentCore Runtime\n\n{text}\n\n---\n"
                f"Reviewed commit `{job['sha'][:7]}` in a new session of its own (`{session[-12:]}`), "
                f"{seconds:.0f} s after the pull request event. The agent read only the diff; it never "
                "held a GitHub token.")
    else:
        body = (f"The review session ended without a review (exit {code}). The agent's last "
                f"output:\n\n```\n{stdout[-1500:]}\n```")
    tool(url, "comment_on_issue", {**where, "body": body})
    result = {"repo": f"{job['owner']}/{job['repo']}", "number": job["number"], "exit": code,
              "reviewed": bool(code == 0 and text), "seconds": round(seconds, 1), "session": session}
    # One log line per finished review; progress.py reads it to report the review step.
    print("REVIEW_POSTED " + json.dumps(result))
    return result
