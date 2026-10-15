"""Check the game guidance delivered by image staging and Runtime archives."""
from __future__ import annotations

import io
from pathlib import Path
import shlex
import tarfile
from types import SimpleNamespace

import harness_config
import roles
import runtime_stage


ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "harness-skills/skills/backend-engineering/SKILL.md"


def assert_game_owned_scoring(text):
    for legacy_requirement in (
        "0 to 1000", "GET /api/scores", "POST /api/scores", "host reporter",
    ):
        assert legacy_requirement not in text
    normalized = " ".join(text.split())
    assert "how actual play earns a score" in normalized
    assert "Persist scores across service restarts." in normalized
    assert "API routes and payloads" in normalized
    assert "Preserve existing interfaces and saved data" in normalized
    assert "without comparing scores or ranking teams" in normalized
    assert "completed round and play again" in normalized
    assert "STAY IN THE FOREGROUND" in normalized
    assert "`PORT`" in normalized


def assert_published_browser_contract(text):
    normalized = " ".join(text.split())
    for requirement in (
        "scripts and pointer lock",
        "opaque origin",
        "Persist durable game scores on the server",
        "localStorage",
        "sessionStorage",
        "service workers",
        "JavaScript `fetch`",
        "`submit` event",
        "`event.preventDefault()`",
        "`allow-forms`",
        "`form-action 'none'`",
        "blocks native form navigation",
        "popups",
        "new tabs",
        "browser dialogs",
        "Cookie",
        "Authorization",
        "/play/app/",
        "relative",
        "CORS",
        "README",
    ):
        assert requirement in normalized
    assert "native form submission is unavailable" not in normalized
    assert "separately published copy at a public game origin" not in normalized


def test_actual_backend_image_copies_the_game_owned_scoring_skill():
    # The image reads the skill through the "skills" named context (docker-bake.hcl and
    # _build_push.sh both point it at harness-skills/skills), so the bytes the image gets
    # are exactly SKILL: there is no staged copy that could drift.
    bake = (ROOT / "docker-bake.hcl").read_text()
    assert 'skills = "harness-skills/skills"' in bake
    build = (ROOT / "coding-agents/_build_push.sh").read_text()
    assert "skills=${tools_dir}/../harness-skills/skills" in build
    copies = [
        shlex.split(line)
        for line in (ROOT / "coding-agents/claude-code/Dockerfile").read_text().splitlines()
        if line.startswith("COPY ")
    ]
    skill_copy = next(c for c in copies if "--from=skills" in c)
    assert skill_copy[-2:] == ["backend-engineering/", "/home/agent/skills/backend-engineering/"]
    assert (ROOT / "harness-skills/skills" / skill_copy[-2] / "SKILL.md") == SKILL
    assert (ROOT / "coding-agents/claude-code/CLAUDE.md").read_bytes() == (
        ROOT / "orchestrator/harness/claude-code/CLAUDE.md"
    ).read_bytes()
    assert_game_owned_scoring(SKILL.read_text())
    assert_published_browser_contract(SKILL.read_text())


def test_actual_runtime_archive_contains_game_owned_scoring(tmp_path, monkeypatch):
    backend = next(iter(roles.by_capability("backend")))
    steering = Path(harness_config.harness_file(backend))
    setup = harness_config.parse_setup_spec(str(steering))
    declared = [str((steering.parent / path).resolve()) for path in setup["skills"]]
    assert str(SKILL.parent) in declared
    writes = []
    monkeypatch.delenv("WORKSHOP_S3FILES_DIR", raising=False)
    monkeypatch.setenv("WORKSHOP_RUNTIME_BUCKET", "offline-backend-game-skill")
    monkeypatch.setattr(
        runtime_stage, "_client",
        lambda region: SimpleNamespace(put_object=lambda **kwargs: writes.append(kwargs)),
    )

    assert runtime_stage.stage_skills(
        "run_backend_game_skill", declared, region="us-east-1", agent_id=backend,
    ) > 0
    assert len(writes) == 1
    assert writes[0]["Key"] == runtime_stage.archive_key(
        runtime_stage.skills_subdir("run_backend_game_skill", backend),
    )
    payload = writes[0]["Body"]
    (tmp_path / "actual-skills.tar.gz").write_bytes(payload)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        delivered = archive.extractfile("skills/backend-engineering/SKILL.md").read()
    assert delivered == SKILL.read_bytes()
    assert_game_owned_scoring(delivered.decode())
    assert_published_browser_contract(delivered.decode())
