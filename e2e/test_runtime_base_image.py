"""Keep every selected Runtime base consistent with the canonical toolchain."""
import json
from pathlib import Path
import re
import shlex

import pytest


ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / "coding-agents/cli-versions.json"
ROLES = ("claude-code", "claude-code-validator", "codex", "kiro", "opencode")


def runtime_base(dockerfile):
    # The MCP Dockerfile also has a uv tool stage; inspect its final Runtime stage.
    return [
        line.split()[1]
        for line in dockerfile.read_text().splitlines()
        if line.startswith("FROM ")
    ][-1]


@pytest.mark.parametrize("role", ROLES)
def test_role_base_defaults_to_the_manifest_pin_and_builds_from_named_contexts(role):
    folder = ROOT / "coding-agents" / role
    dockerfile = (folder / "Dockerfile").read_text()
    expected = json.loads(PINS.read_text())["runtime"]["base_image"]
    assert runtime_base(folder / "Dockerfile") == "${WORKSHOP_RUNTIME_BASE_IMAGE}"
    assert f"ARG WORKSHOP_RUNTIME_BASE_IMAGE={expected}" in dockerfile.splitlines()
    assert "COPY --from=toolchain cli-versions.json cli_versions.py /opt/workshop-cli/" in dockerfile
    assert "_toolchain" not in dockerfile
    setup = (folder / "setup.sh").read_text().replace("\\\n", "")
    calls = re.findall(r"^\s*build_and_push_arm64[^\n]+", setup, re.MULTILINE)
    assert len(calls) == 1 and calls[0].rstrip().endswith("--toolchain")


@pytest.mark.parametrize("role", ROLES)
def test_every_copy_input_exists_and_local_state_stays_out_of_the_context(role):
    folder = ROOT / "coding-agents" / role
    contexts = {"toolchain": ROOT / "coding-agents", "skills": ROOT / "harness-skills/skills"}
    for line in (folder / "Dockerfile").read_text().splitlines():
        if not line.startswith("COPY "):
            continue
        words = shlex.split(line)[1:]
        source_root = folder
        for word in words:
            if word.startswith("--from="):
                source_root = contexts[word.split("=", 1)[1]]
        for source in [w for w in words if not w.startswith("--")][:-1]:
            assert list(source_root.glob(source)), f"{role}: COPY input missing: {source}"
    ignored = (folder / ".dockerignore").read_text().split()
    for pattern in ("runtime_config.json", "agent.config", "*.private.json", "*.pem"):
        assert pattern in ignored, f"{role}/.dockerignore must exclude {pattern}"


def test_the_bake_file_builds_every_role_with_the_same_pin_and_contexts():
    bake = (ROOT / "docker-bake.hcl").read_text()
    expected = json.loads(PINS.read_text())["runtime"]["base_image"]
    assert f'default = "{expected}"' in bake
    assert 'contexts  = { toolchain = "coding-agents", skills = "harness-skills/skills" }' in bake
    assert 'platforms = ["linux/arm64"]' in bake
    for role in ROLES:
        assert f'target "{role}"' in bake and f'context  = "coding-agents/{role}"' in bake
        assert f'"${{REGISTRY}}/coding-agents-{role}:latest"' in bake


@pytest.mark.parametrize("path", (
    "coding-agents/gateway_mcp/app/Dockerfile",
    "orchestrator-agent/Dockerfile",
))
def test_separately_built_runtime_base_matches_the_manifest(path):
    # These have separate build systems. Keep their explicit final-stage pins
    # synchronized without expanding their build context or changing the uv tool.
    expected = json.loads(PINS.read_text())["runtime"]["base_image"]
    assert runtime_base(ROOT / path) == expected, (
        f"{path} must track coding-agents/cli-versions.json runtime.base_image"
    )
