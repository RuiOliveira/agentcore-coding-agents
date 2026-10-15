"""A Runtime created in the AgentCore console is wired by its NAME.

Parts 1 and 4 of the workshop create every Runtime in the console, which writes no
runtime_config.json anywhere. Before this rung the host console (Agent Studio) saw
every role as unwired: its Agents page could not open a terminal and Chat could not
dispatch, although the Runtimes existed and progress.py and the coordinator deploy
already found them by name. The listing is the only AWS call, so it is replaced with
a fake here; the suite never lists a real account.
"""
from __future__ import annotations

import json
import os

import pytest

import runtime_config

_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/{}-AbCdEf1234"


def _summary(name: str, status: str = "READY") -> dict:
    return {"agentRuntimeName": name, "agentRuntimeArn": _ARN.format(name), "status": status}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSHOP_RUNTIME_CONFIG", str(tmp_path / "runtime.local.json"))
    monkeypatch.setenv("WORKSHOP_CODING_AGENTS_DIR", str(tmp_path / "coding-agents"))
    monkeypatch.setenv("WORKSHOP_ROLES", "claude-code,codex,kiro")
    monkeypatch.setenv("WORKSHOP_RUNTIME_NAME_DISCOVERY", "1")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    for role in runtime_config.roles():
        monkeypatch.delenv(runtime_config._env_key(role), raising=False)
    runtime_config.forget_named_runtimes()
    yield
    runtime_config.forget_named_runtimes()


@pytest.fixture
def listing(monkeypatch):
    """The account's Runtimes as ListAgentRuntimes would page them, and a call count."""
    state = {"runtimes": [_summary("claude_code"), _summary("codex"), _summary("kiro")],
             "calls": 0, "error": None}

    def fake(region):
        state["calls"] += 1
        assert region == "us-west-2"
        if state["error"]:
            raise state["error"]
        return list(state["runtimes"])

    monkeypatch.setattr(runtime_config, "_list_runtimes", fake)
    return state


def test_a_console_created_runtime_is_wired_by_its_name(listing):
    assert runtime_config.instances("claude-code") == [(_ARN.format("claude_code"), "named")]
    row = next(r for r in runtime_config.status()["roles"] if r["role"] == "kiro")
    assert row["wired"] and row["source"] == "named"
    # One listing answers every role for a while: the console polls this view.
    assert listing["calls"] == 1


def test_a_local_file_or_a_setting_still_wins(listing, tmp_path):
    agents = tmp_path / "coding-agents" / "codex"
    agents.mkdir(parents=True)
    deployed = _ARN.format("codex_from_deploy_py")
    (agents / "runtime_config.json").write_text(json.dumps({"runtime_arn": deployed}))
    assert runtime_config.instances("codex") == [(deployed, "deployed")]
    saved = _ARN.format("pasted")
    runtime_config.save_runtime("claude-code", saved)
    assert runtime_config.instances("claude-code") == [(saved, "settings")]


def test_only_a_serving_runtime_is_a_target(listing):
    listing["runtimes"] = [_summary("claude_code", "CREATING"), _summary("codex", "UPDATING"),
                           _summary("kiro", "CREATE_FAILED")]
    assert runtime_config.instances("claude-code") == []
    assert runtime_config.instances("codex") == [(_ARN.format("codex"), "named")]
    assert runtime_config.instances("kiro") == []


def test_a_listing_that_fails_reads_as_unwired_without_a_request_storm(listing):
    listing["error"] = RuntimeError("AccessDeniedException")
    for _ in range(5):
        assert runtime_config.resolve("claude-code") is None
    assert listing["calls"] == 1


def test_a_runtime_created_a_moment_ago_appears_after_the_cache_is_dropped(listing):
    listing["runtimes"] = []
    assert runtime_config.resolve("kiro") is None
    listing["runtimes"] = [_summary("kiro")]
    runtime_config.forget_named_runtimes()
    assert runtime_config.resolve("kiro") == (_ARN.format("kiro"), "named")


def test_discovery_can_be_switched_off(listing, monkeypatch):
    monkeypatch.setenv("WORKSHOP_RUNTIME_NAME_DISCOVERY", "0")
    assert runtime_config.instances("claude-code") == []
    assert listing["calls"] == 0


def test_the_orchestrator_is_never_looked_up_by_name(listing):
    listing["runtimes"] = [_summary("orchestrator")]
    assert runtime_config.named_runtime("orchestrator") == ""
    assert listing["calls"] == 0


def test_a_named_runtime_cannot_be_removed_from_settings(listing):
    out = runtime_config.remove_runtime("claude-code", _ARN.format("claude_code"))
    assert "AgentCore console" in out["error"]

