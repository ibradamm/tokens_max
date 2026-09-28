import json
import os
import shutil
import subprocess

import pytest

from server import agent_skills, ponytail
from server.common import VENDOR, safe_join
from server.graphify_server import parse_repo

NODE = shutil.which("node")
MODES = [None, "", "lite", "full", "ultra", "off", "review", "LITE ", "bogus"]


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize("mode", MODES)
def test_ponytail_ruleset_matches_upstream_js(mode):
    js = (
        "const {getPonytailInstructions}=require(process.argv[1]);"
        "const m=JSON.parse(process.argv[2]);process.stdout.write(getPonytailInstructions(m===null?undefined:m));"
    )
    hook = str(VENDOR / "ponytail" / "hooks" / "ponytail-instructions.js")
    env = {k: v for k, v in os.environ.items() if k != "PONYTAIL_DEFAULT_MODE"}
    out = subprocess.run([NODE, "-e", js, hook, json.dumps(mode)], capture_output=True, text=True, check=True, env=env)
    assert ponytail.get_ponytail_instructions(mode) == out.stdout


def test_resolve_mode_follows_upstream_mcp(monkeypatch):
    monkeypatch.delenv("PONYTAIL_DEFAULT_MODE", raising=False)
    assert [ponytail.resolve_mode(m) for m in (None, "off", "ultra", "x")] == ["full", "full", "ultra", "full"]
    monkeypatch.setenv("PONYTAIL_DEFAULT_MODE", "lite")
    assert ponytail.resolve_mode(None) == "lite"


def test_agent_skills_inventory():
    assert len(agent_skills.SKILLS) == 25 and len(agent_skills.COMMANDS) == 9
    assert len(agent_skills.AGENTS) == 4 and len(agent_skills.REFERENCES) == 7
    assert all(d.description for d in agent_skills.SKILLS.values())


def test_command_appends_invoked_skills():
    text = agent_skills.render_command("review")
    assert "# Skill: code-review-and-quality" in text


def test_safe_join_blocks_traversal(tmp_path):
    (tmp_path / "a").write_text("x")
    assert safe_join(tmp_path, "a").read_text() == "x"
    for bad in ("../etc/passwd", "/etc/passwd", "a/../../x"):
        with pytest.raises(ValueError):
            safe_join(tmp_path, bad)


def test_parse_repo():
    assert parse_repo("owner/repo")[1:] == ("owner/repo", "owner/repo")
    assert parse_repo("https://github.com/o/r.git")[0] == "https://github.com/o/r.git"
    assert parse_repo("https://github.com/o/r/tree/main")[1] == "o/r"
    assert parse_repo("https://gitlab.com/g/sub/r")[1] == "gitlab.com/g/sub/r"
    for bad in ("http://github.com/o/r", "https://evil.example/o/r", "file:///etc", "https://user:pw@github.com/o/r",
                "https://github.com/o/..", "https://10.0.0.1/o/r", "https://github.com:8443/o/r"):
        with pytest.raises(ValueError):
            parse_repo(bad)
