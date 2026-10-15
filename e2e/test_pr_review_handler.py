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
import time
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "coding-agents" / "pr_review" / "handler.py"
spec = importlib.util.spec_from_file_location("pr_review_handler", SOURCE)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def signed(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _now(offset_s: float = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_s))


def pr_event(action="opened", draft=False, state="open", updated=None) -> dict:
    return {"action": action, "repository": {"name": "game", "owner": {"login": "octo"}},
            "pull_request": {"number": 7, "title": 'Add "pause"', "draft": draft, "state": state,
                             "updated_at": updated or _now(), "author_association": "OWNER",
                             "user": {"login": "dev"}, "head": {"sha": "abc1234def"}}}


class Ctx:
    invoked_function_arn = "arn:aws:lambda:us-west-2:111122223333:function:stack-pr-review"


class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)


class FakeTable:
    """DynamoDB's conditional put: the first writer of an id wins."""
    class exceptions:
        class ConditionalCheckFailedException(Exception):
            pass

    def __init__(self):
        self.ids = set()
        self.broken = None

    def put_item(self, TableName, Item, ConditionExpression):
        assert ConditionExpression == "attribute_not_exists(id)"
        if self.broken:
            raise self.broken
        if Item["id"]["S"] in self.ids:
            raise self.exceptions.ConditionalCheckFailedException()
        self.ids.add(Item["id"]["S"])

    def delete_item(self, TableName, Key):
        self.ids.discard(Key["id"]["S"])


class FakeSession:
    def __init__(self, lam, table=None):
        self.lam, self.table = lam, table or FakeTable()

    def client(self, name, **_):
        assert name in ("lambda", "dynamodb"), name
        return self.lam if name == "lambda" else self.table


@pytest.fixture
def lam(monkeypatch):
    fake = FakeLambda()
    fake.session = FakeSession(fake)
    monkeypatch.setattr(hook, "_session", fake.session)
    monkeypatch.setattr(hook, "webhook_secret", lambda: "s3cret")
    monkeypatch.setenv("DELIVERIES_TABLE", "coding-agents-pr-review-deliveries")
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


def test_a_team_builds_pull_request_is_left_to_its_own_gate_and_review(lam):
    """Part 4's coordinator opens its pull requests from workshop/runs/... branches in
    the same repository part 3 connected; each already has an executed check and an
    independent Assessment, so the event reviewer adds no second verdict."""
    team = pr_event()
    team["pull_request"]["head"]["ref"] = "workshop/runs/run-153232-c415c8e32151/codex-4920a1"
    assert call(team, "pull_request")["statusCode"] == 202 and lam.calls == []
    person = pr_event()
    person["pull_request"]["head"]["ref"] = "add-pause"
    call(person, "pull_request")
    assert len(lam.calls) == 1


def test_a_signed_event_is_acted_on_once(lam):
    """A redelivery or a replay of the same signed body starts nothing new."""
    body = pr_event()
    assert call(body, "pull_request")["statusCode"] == 202
    again = call(body, "pull_request")
    assert again["statusCode"] == 202 and "already handled" in again["body"]
    assert len(lam.calls) == 1


def test_a_stranger_s_pull_request_never_reaches_a_session(lam):
    """The review runs the change; a fork from someone the repository does not trust must
    not get a microVM with the Runtime's role and the shared mount."""
    for association in ("NONE", "FIRST_TIME_CONTRIBUTOR", "CONTRIBUTOR", None):
        event = pr_event()
        event["pull_request"]["author_association"] = association
        assert call(event, "pull_request")["statusCode"] == 202
    assert lam.calls == []


def _from(event, head_repo, association, login="dev"):
    event["repository"]["full_name"] = "octo/game"
    event["pull_request"]["head"]["repo"] = {"full_name": head_repo} if head_repo else None
    event["pull_request"]["author_association"] = association
    event["pull_request"]["user"] = {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"}
    return event


def test_the_fixers_own_pull_request_is_reviewed(lam):
    """The App opens the fixer's pull request from a branch of the same repository; its
    association is not OWNER, and a different session must still review the fix."""
    assert call(_from(pr_event(), "octo/game", "NONE", "workshop-app[bot]"), "pull_request")["statusCode"] == 202
    assert len(lam.calls) == 1


def test_a_strangers_fork_is_not_reviewed_but_an_owners_fork_is(lam):
    call(_from(pr_event(), "stranger/game", "NONE", "stranger"), "pull_request")
    call(_from(pr_event(), "stranger/game", "FIRST_TIME_CONTRIBUTOR", "stranger"), "pull_request")
    assert lam.calls == []
    call(_from(pr_event(), "octo-fork/game", "OWNER", "octo"), "pull_request")
    assert len(lam.calls) == 1


def test_a_failed_hand_off_can_be_redelivered(lam):
    """If the asynchronous hand-off raises, the digest is forgotten, so GitHub's
    Redeliver of the same body is acted on instead of answered 'already handled'."""
    body = pr_event()

    def refuse(**kw):
        raise RuntimeError("TooManyRequestsException")

    lam.invoke = refuse
    with pytest.raises(RuntimeError):
        call(body, "pull_request")
    assert lam.session.table.ids == set()
    del lam.invoke
    assert call(body, "pull_request")["statusCode"] == 202 and len(lam.calls) == 1


def test_an_unreachable_table_answers_503_not_a_crash(lam):
    class ClientError(Exception):
        response = {"Error": {"Code": "AccessDeniedException"}}

    lam.session.table.broken = ClientError("denied")
    r = call(pr_event(), "pull_request")
    assert r["statusCode"] == 503 and lam.calls == []


def test_a_pull_request_from_a_deleted_fork_is_not_reviewed(lam):
    call(_from(pr_event(), None, "NONE", "gone"), "pull_request")
    assert lam.calls == []


def test_a_stale_signed_event_is_ignored(lam):
    """GitHub's signature carries no time, so the event's own time bounds a replay."""
    r = call(pr_event(updated=_now(-2 * hook.REPLAY_WINDOW_S)), "pull_request")
    assert r["statusCode"] == 202 and "replay window" in r["body"]
    no_time = pr_event()
    del no_time["pull_request"]["updated_at"]
    call(no_time, "pull_request")
    assert lam.calls == []


def test_the_endpoint_reads_the_webhook_secret_never_the_app_key():
    assert hook.SECRET_ID == "agentcore/github-mcp/webhook"


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
                                        "created_at": _now(),
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


def test_the_team_branch_prefix_matches_the_engine():
    source = (SOURCE.parents[2] / "orchestrator" / "work_items.py").read_text()
    assert 'branch=f"' + hook.TEAM_BUILD_BRANCH in source


def test_rules_come_from_the_default_branch_so_a_stacked_fix_is_judged_by_them(monkeypatch):
    """Part 3's /fix opens a pull request whose base is the feature branch, which was
    created before REVIEW.md landed on main; reading the rules from that base gave the
    fix's reviewer no rules at all."""
    asked = []

    def tool(url, name, args, raw=False):
        asked.append((name, args.get("path"), args.get("ref")))
        return "# rules" if args.get("path") == hook.RULES_FILE else "code"

    monkeypatch.setattr(hook, "tool", tool)
    job = hook.job_from_event("pull_request", {
        **pr_event(), "repository": {"name": "game", "owner": {"login": "octo"},
                                     "default_branch": "main"}})
    assert job["default_branch"] == "main"
    pr = {"files": [{"filename": "discount.py", "status": "modified"}], "head_sha": "abc",
          "base": "add-pause"}
    _files, rules = hook.gather("https://gw", job, pr)
    assert rules == "# rules"
    assert (("get_file", hook.RULES_FILE, "main")) in asked
