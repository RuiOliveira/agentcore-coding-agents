"""The pull request reviewer: signature, event filter, hand-off, command size, and output.

Runs offline with stubbed AWS clients. The function itself is
``coding-agents/pr_review/handler.py``; the workshop template embeds that exact file.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import shlex
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "coding-agents" / "pr_review" / "handler.py"
spec = importlib.util.spec_from_file_location("pr_review_handler", SOURCE)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def signed(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def pr_event(action="opened", draft=False, state="open") -> dict:
    return {"action": action, "repository": {"name": "game", "owner": {"login": "octo"}},
            "pull_request": {"number": 7, "title": 'Add "pause"', "draft": draft, "state": state,
                             "user": {"login": "dev"}, "head": {"sha": "abc1234def"}}}


class Ctx:
    invoked_function_arn = "arn:aws:lambda:us-west-2:111122223333:function:stack-pr-review"


class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)


class FakeSession:
    def __init__(self, lam):
        self.lam = lam

    def client(self, name, **_):
        assert name == "lambda", name
        return self.lam


@pytest.fixture
def lam(monkeypatch):
    fake = FakeLambda()
    monkeypatch.setattr(hook, "_session", FakeSession(fake))
    monkeypatch.setattr(hook, "webhook_secret", lambda: "s3cret")
    return fake


def call(body: dict, kind: str, secret="s3cret", signature=None):
    raw = json.dumps(body).encode()
    event = {"headers": {"X-GitHub-Event": kind,
                         "X-Hub-Signature-256": signed(secret, raw) if signature is None else signature},
             "body": base64.b64encode(raw).decode(), "isBase64Encoded": True}
    return hook.handler(event, Ctx())


def test_a_signed_pull_request_is_handed_off_and_answered_at_once(lam):
    r = call(pr_event(), "pull_request")
    assert r["statusCode"] == 202 and len(lam.calls) == 1
    assert lam.calls[0]["InvocationType"] == "Event"
    job = json.loads(lam.calls[0]["Payload"])["job"]
    assert job["mode"] == "review"
    assert (job["owner"], job["repo"], job["number"], job["sha"]) == ("octo", "game", 7, "abc1234def")


def test_a_wrong_or_missing_signature_is_refused(lam):
    assert call(pr_event(), "pull_request", signature="sha256=" + "0" * 64)["statusCode"] == 401
    assert call(pr_event(), "pull_request", signature="")["statusCode"] == 401
    assert lam.calls == []


def test_only_a_reviewable_pull_request_starts_a_session(lam):
    for event, kind in ((pr_event("closed"), "pull_request"), (pr_event(draft=True), "pull_request"),
                        (pr_event(state="closed"), "pull_request"), ({"action": "created"}, "issue_comment")):
        assert call(event, kind)["statusCode"] == 202
    assert call({"zen": "x"}, "ping")["statusCode"] == 200
    assert lam.calls == []
    assert call(pr_event("synchronize"), "pull_request")["statusCode"] == 202 and len(lam.calls) == 1


def test_no_secret_means_refuse_never_accept_unsigned(lam, monkeypatch):
    monkeypatch.setattr(hook, "webhook_secret", lambda: "")
    assert call(pr_event(), "pull_request")["statusCode"] == 503
    assert lam.calls == []


def fix_comment(body="/fix please", association="OWNER", user_type="User", on_pr=True):
    issue = {"number": 7, "title": "Add pause"}
    if on_pr:
        issue["pull_request"] = {"url": "x"}
    return {"action": "created", "repository": {"name": "game", "owner": {"login": "octo"}},
            "issue": issue, "comment": {"body": body, "author_association": association,
                                        "user": {"login": "dev", "type": user_type}}}


def test_a_trusted_fix_comment_starts_a_fixer(lam):
    assert call(fix_comment("/fix keep it short"), "issue_comment")["statusCode"] == 202
    job = json.loads(lam.calls[0]["Payload"])["job"]
    assert job["mode"] == "fix" and job["number"] == 7 and job["request"] == "keep it short"


def test_untrusted_bot_and_ordinary_comments_start_nothing(lam):
    for event in (fix_comment(association="NONE"), fix_comment(user_type="Bot"),
                  fix_comment(body="looks good"), fix_comment(on_pr=False)):
        assert call(event, "issue_comment")["statusCode"] == 202
    assert lam.calls == [], "only a person with write access can wake a fixer"


def test_the_bundle_round_trips_and_fits_one_command():
    files = {f"src{i}.py": "x = 1\n" * 4000 + f"# {i}\n" for i in range(12)}
    encoded, dropped = hook.fit_bundle("+x", files, {"RULES.md": "Use Decimal for money."})
    assert len(encoded) <= hook.MAX_BUNDLE_CHARS
    job = {"owner": "octo", "repo": "game", "number": 7, "title": 'Add "pause" mode', "author": "dev"}
    for mode in ("review", "fix"):
        command = hook.build_command(job, encoded, mode=mode)
        assert len(command) < 65_536, "InvokeAgentRuntimeCommand accepts at most 65,536 characters"
    inner = shlex.split(hook.build_command(job, encoded, mode="fix"))[2]
    assert "WORKSHOP_AGENT_WORKDIR=/tmp/work" in inner and hook.FILES_BEGIN in inner
    import base64 as b64, io as _io, tarfile as _tar
    with _tar.open(fileobj=_io.BytesIO(b64.b64decode(encoded)), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert {"pr.diff", "RULES.md"} <= names
    assert all(f"src/{p}" in names for p in files if p not in dropped)


def test_a_too_large_change_drops_the_largest_files_first():
    import random
    rnd = random.Random(7)
    noise = lambda n: "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(n))
    files = {"big.py": noise(90_000), "small.py": "y = 2\n"}
    encoded, dropped = hook.fit_bundle("+y", files, {})
    assert dropped == ["big.py"] and len(encoded) <= hook.MAX_BUNDLE_CHARS


def test_the_fixers_files_are_read_back_from_its_output():
    out_tar = hook.bundle({"src/discount.py": "def f(): return 1\n", "src/../escape.py": "x", "notes.md": "n"})
    stdout = f"noise\n{hook.BEGIN}\n- discount.py: fixed\n{hook.END}\n{hook.FILES_BEGIN}\n{out_tar}\n{hook.FILES_END}\n"
    assert hook.extract_files(stdout) == {"discount.py": "def f(): return 1\n"}
    assert hook.extract_review(stdout) == "- discount.py: fixed"
    assert hook.extract_files("no files") == {}


def test_the_latest_review_is_the_one_the_fixer_reads():
    comments = [{"body": hook.REVIEW_HEADING + "\nold"}, {"body": "chat"},
                {"body": hook.REVIEW_HEADING + "\nnew"}, {"body": "Opened #9"}]
    assert hook.latest_review(comments).endswith("new")
    assert hook.latest_review([]) == ""


def test_the_diff_carries_new_file_line_numbers():
    patch = "@@ -1,3 +1,4 @@\n def f():\n-    return 1\n+    x = 2\n+    return x\n \n\\ No newline at end of file"
    numbered = hook.number_patch(patch).splitlines()
    assert numbered[0].startswith("@@")
    assert numbered[1] == "   1  def f():"
    assert numbered[2] == "     -    return 1"
    assert numbered[3] == "   2 +    x = 2" and numbered[4] == "   3 +    return x"
    assert numbered[6].startswith("\\")
    assert "   1 +def apply" in hook.build_diff({"files": [{"filename": "d.py", "status": "added", "additions": 1,
                                                         "deletions": 0, "patch": "@@ -0,0 +1 @@\n+def apply(): pass"}]})


def test_the_review_is_read_between_its_markers():
    out = f"noise\n{hook.BEGIN}\nLooks fine.\n\nVerdict: looks good\n{hook.END}\ntrailing"
    assert hook.extract_review(out) == "Looks fine.\n\nVerdict: looks good"
    assert hook.extract_review("no markers") == ""


def test_the_review_session_does_not_claim_a_kernel_identity():
    command = hook.build_command({"owner": "o", "repo": "r", "number": 1, "title": "t", "author": "a"}, "+x")
    assert "boot_id" not in command, "sessions share a boot ID; it identifies nothing"
