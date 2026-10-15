"""Agent Studio reads the attendee's clone, not the host's platform copy.

In the workshop the console runs from PlatformFolder (/opt/workshop), while Get in
clones the code into the attendee's home and every later setup step writes there:
connect-github.sh saves the repository and Gateway into the clone's .runs, and the
Gateway deploy writes its state beside its scripts. A console that read its own
platform copy saw no repository and no Gateway, so Chat could not inspect or open a
pull request. ``adopt_attendee_clone`` points those three settings at the clone.
"""
from __future__ import annotations

import importlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for p in (_HERE, os.path.join(_REPO, "orchestrator"),
          os.path.join(_REPO, "interactive-api"), os.path.join(_REPO, "metrics-api")):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("WORKSHOP_GITHUB_STORE", "local")
server = importlib.import_module("server")


def _clone(tmp_path):
    home = tmp_path / "home"
    clone = home / server.ATTENDEE_CLONE_NAME
    (clone / "orchestrator").mkdir(parents=True)
    platform = tmp_path / "opt" / server.ATTENDEE_CLONE_NAME
    (platform / "orchestrator").mkdir(parents=True)
    return home, clone, platform


def test_the_console_reads_the_clone_in_the_attendees_home(tmp_path):
    home, clone, platform = _clone(tmp_path)
    env = {"WORKSHOP_DEV_ROOT": str(home)}
    assert server.adopt_attendee_clone(env, str(platform)) == str(clone)
    assert env["WORKSHOP_GITHUB_SETTINGS"] == str(clone / ".runs" / "github_gateway.local.json")
    assert env["WORKSHOP_GATEWAY_STATE"] == str(
        clone / "coding-agents" / "gateway_mcp" / ".deployed-state.json")
    assert env["WORKSHOP_CODING_AGENTS_DIR"] == str(clone / "coding-agents")


def test_an_explicit_setting_still_wins(tmp_path):
    home, clone, platform = _clone(tmp_path)
    env = {"WORKSHOP_DEV_ROOT": str(home), "WORKSHOP_GITHUB_SETTINGS": "/elsewhere.json"}
    server.adopt_attendee_clone(env, str(platform))
    assert env["WORKSHOP_GITHUB_SETTINGS"] == "/elsewhere.json"


def test_nothing_changes_before_the_clone_exists_or_when_running_from_it(tmp_path):
    home, clone, platform = _clone(tmp_path)
    env = {"WORKSHOP_DEV_ROOT": str(tmp_path / "empty-home")}
    assert server.adopt_attendee_clone(env, str(platform)) == ""
    assert set(env) == {"WORKSHOP_DEV_ROOT"}
    env = {"WORKSHOP_DEV_ROOT": str(home)}
    assert server.adopt_attendee_clone(env, str(clone)) == ""
    assert set(env) == {"WORKSHOP_DEV_ROOT"}


def test_an_explicit_clone_path_is_honoured(tmp_path):
    _home, clone, platform = _clone(tmp_path)
    env = {"WORKSHOP_ATTENDEE_CLONE": str(clone)}
    assert server.adopt_attendee_clone(env, str(platform)) == str(clone)
