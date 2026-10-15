"""A Claude Code launcher must trust the directory it is about to work in.

Found on a live console-dispatched run (2026-09-02): the image pre-trusts only
/home/agent, and the interactive TUI asks "Is this a project you trust?" for any other
directory with "No, exit" preselected. The orchestrator pasted its prompt into that
dialog, the Enter answered "No, exit", the TUI quit to a shell prompt, and the engine
reported the role "finished but changed nothing". The headless --print path never
shows the dialog, so only the console's interactive path broke.

Both Claude Code launchers must therefore mark their working directory trusted in
~/.claude.json before starting the CLI. This runs each launcher's real settings block
against a throwaway HOME rather than grepping for a string.
"""
import json
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LAUNCHERS = {
    "claude-code": os.path.join(ROOT, "coding-agents", "claude-code", "run.sh"),
    "claude-code-validator": os.path.join(
        ROOT, "coding-agents", "claude-code-validator", "run.sh"),
}


def _settings_block(path: str) -> str:
    """The Python the launcher feeds to `python3 -` for its first-run settings."""
    src = open(path, encoding="utf-8").read()
    # The opener line carries redirections after the heredoc word, so match to EOL.
    blocks = re.findall(r"<<'PYSETTINGS'[^\n]*\n(.*?)\nPYSETTINGS\n", src, re.S)
    assert blocks, f"{path} has no PYSETTINGS block"
    return "\n".join(blocks)


@pytest.mark.parametrize("role", sorted(LAUNCHERS))
def test_the_launcher_trusts_the_worktree_it_is_given(role, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    workdir = "/tmp/workshop-0123456789ab"
    env = dict(os.environ, HOME=str(home), CLAUDE_PROJECT_DIR=workdir)
    proc = subprocess.run([sys.executable, "-"], input=_settings_block(LAUNCHERS[role]),
                          text=True, env=env, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    cfg = json.load(open(home / ".claude.json"))
    project = cfg["projects"][workdir]
    assert project["hasTrustDialogAccepted"] is True, \
        "the TUI would otherwise open on a trust dialog with 'No, exit' preselected"
    assert project["hasCompletedProjectOnboarding"] is True


def _resolve_run_dir(cwd, workdir=None) -> str:
    """Run the launcher's own RUN_DIR block in bash from ``cwd`` and print the result."""
    src = open(LAUNCHERS["claude-code"], encoding="utf-8").read()
    block = src[src.index('if [ -n "${WORKSHOP_AGENT_WORKDIR:-}" ]; then'):]
    block = block[:block.index("\nfi\n") + 4]
    env = {k: v for k, v in os.environ.items() if k != "WORKSHOP_AGENT_WORKDIR"}
    env["HOME"] = "/home/agent"
    if workdir:
        env["WORKSHOP_AGENT_WORKDIR"] = workdir
    proc = subprocess.run(["bash", "-c", block + 'printf %s "$RUN_DIR"'], cwd=cwd, env=env,
                          text=True, capture_output=True, check=True)
    return proc.stdout


def test_the_backend_launcher_resolves_its_directory_before_trusting_it(tmp_path):
    """RUN_DIR must be known when the settings block runs, or it trusts the wrong
    directory. It is WORKSHOP_AGENT_WORKDIR on a dispatched turn; by hand it is the
    folder the person changed into, or HOME from a fresh shell at /."""
    src = open(LAUNCHERS["claude-code"], encoding="utf-8").read()
    run_dir_at = src.index('RUN_DIR="$WORKSHOP_AGENT_WORKDIR"')
    trust_at = src.index("hasTrustDialogAccepted")
    assert run_dir_at < trust_at
    assert 'CLAUDE_PROJECT_DIR="$RUN_DIR" python3 -' in src

    folder = str(tmp_path.resolve())
    assert _resolve_run_dir(folder, "/tmp/workshop-0123456789ab") == "/tmp/workshop-0123456789ab"
    assert _resolve_run_dir(folder) == folder
    assert _resolve_run_dir("/") == "/home/agent"
