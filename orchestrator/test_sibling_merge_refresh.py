"""A pull request whose base moved under it gets its ONE bounded refresh.

Live run run_052015_faadb1b489d5 (October 13, merge policy auto): Claude Code's backend
merged first, Codex's frontend had changed the same root package.json, README.md and
.gitignore, and nothing refreshed it. Its gate reported STALE_PATCH, the ordinary
repair re-ran Codex on the OLD base, the same stale patch came back, and a pull request
that only needed rebasing ended ROLE_PR_BLOCKED. The per-PR loop now rebases each later
pull request onto the branch a sibling merged into, and gives its owner one refresh
turn when the paths conflict or a declared dependency just merged.
"""
from __future__ import annotations

import os
import time

import engine
import reviewer
from engine import Engine, TERMINAL, public_result
from fixture_executor import FixtureExecutor

_APPROVE = {"approve": True, "reasons": [],
            "assessment": f"**Assessment**: Approve\n\n{reviewer.LGTM_TOKEN}\n",
            "panels": [], "review_unavailable": False}


def _run_two_builders(monkeypatch, tmp_path):
    numbers = iter(range(1, 100))
    turns: list[tuple[str, int, bool]] = []

    def publish(self, run):
        active = run._active_builders if run._active_builders is not None else {
            i.agent for i in run.work_items.values() if i.kind == "builder"}
        for item in run.work_items.values():
            if item.kind == "builder" and item.agent in active:
                item.state, item.stale = "in_review", False
                if not item.pr:
                    n = next(numbers)
                    item.pr = {"pr_url": f"https://github.com/team/app/pull/{n}",
                               "number": n, "base": item.base_branch, "head": item.branch}

    real_produce = FixtureExecutor.produce

    def produce(self, run, agent_id, role):
        path = real_produce(self, run, agent_id, role)
        if agent_id in ("claude-code", "codex"):
            refreshing = "STALE PATCH" in (run._refresh_context or "")
            turns.append((agent_id, run.iterations, refreshing))
            manifest = os.path.join(run.roledir(agent_id), "package.json")
            existing = open(manifest).read() if os.path.exists(manifest) else ""
            line = f'"{agent_id}": "{"reconciled" if refreshing else "first"}"\n'
            # Both builders write the SAME shared manifest, as both did live in an
            # empty repository. On its refresh the later one keeps what merged.
            with open(manifest, "w") as f:
                f.write((existing if refreshing else "") + line)
        return path

    monkeypatch.setattr(Engine, "_publish_active_work_items", publish)
    monkeypatch.setattr(Engine, "_comment_work_item", lambda self, run, item, body: None)
    monkeypatch.setattr(FixtureExecutor, "produce", produce)
    real_assess = reviewer.assess
    monkeypatch.setattr(engine.reviewer, "assess",
                        lambda run, g, n, judge_=None, subject=None, **kw:
                        real_assess(run, g, n, judge=lambda *a: dict(_APPROVE), subject=subject))
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "auto")

    e = Engine(executor_obj=FixtureExecutor())
    try:
        r = e.submit("Use preset=web-app.", ["claude-code", "codex", "kiro"])
        deadline = time.time() + 90
        while r.status not in TERMINAL and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(0.2)
        return r, public_result(r), turns
    finally:
        e.shutdown()


def test_a_later_pull_request_is_refreshed_onto_the_merged_sibling_and_merges(monkeypatch, tmp_path):
    run, result, turns = _run_two_builders(monkeypatch, tmp_path)

    states = {row["agent"]: row.get("state") for row in result["role_prs"]}
    assert states == {"claude-code": "merged", "codex": "merged"}, result.get("fail_reason")
    # Codex ran once to build and exactly once more to reconcile on the new base.
    codex_turns = [t for t in turns if t[0] == "codex"]
    assert [t[2] for t in codex_turns] == [False, True]
    assert [t for t in turns if t[0] == "claude-code"] == [("claude-code", 1, False)]
    merged = open(os.path.join(run.integration_base_dir, "package.json")).read()
    assert '"claude-code": "first"' in merged and '"codex": "reconciled"' in merged
    assert result["status"] == "passed"
