"""Ponytail as a remote MCP server.

Port of upstream ponytail-mcp (DietrichGebert/ponytail, `ponytail-mcp/`), which only
speaks stdio. Same prompt, same tool, same text: the ruleset is built from the vendored
`skills/ponytail/SKILL.md` with the exact mode filter of `hooks/ponytail-instructions.js`
(parity is checked against the JS in tests/test_ponytail.py).

Additions a remote host needs, since claude.ai has no hooks and no plugin skills:
- the ruleset is also sent as the server `instructions` (the closest MCP gets to the
  always-on hook);
- the five companion skills (review, audit, debt, gain, help) are served as prompts and
  through the `ponytail_skill` tool.
"""

from __future__ import annotations

import os
import re
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts.base import Prompt
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from .common import VENDOR, split_frontmatter

PONYTAIL_DIR = VENDOR / "ponytail"
SKILLS_DIR = PONYTAIL_DIR / "skills"

DEFAULT_MODE = "full"
RUNTIME_MODES = ("off", "lite", "full", "ultra")
VALID_MODES = ("off", "lite", "full", "ultra", "review")
MODES = ("lite", "full", "ultra")  # what the server offers; "off" has nothing to serve
COMPANION_SKILLS = ("ponytail-review", "ponytail-audit", "ponytail-debt", "ponytail-gain", "ponytail-help")

_TABLE_LABEL = re.compile(r"^\|\s*\*\*(.+?)\*\*\s*\|")
_EXAMPLE_LABEL = re.compile(r"^-\s*([^:]+):\s*\"")
_JS_FRONTMATTER = re.compile(r"^---[\s\S]*?---\s*")


def normalize_mode(mode) -> str | None:
    if not isinstance(mode, str):
        return None
    m = mode.strip().lower()
    return m if m in RUNTIME_MODES else None


def _normalize_persisted_mode(mode) -> str | None:
    if normalize_mode(mode):
        return normalize_mode(mode)
    if isinstance(mode, str) and mode.strip().lower() in VALID_MODES:
        return mode.strip().lower()
    return None


def get_default_mode() -> str:
    env = os.environ.get("PONYTAIL_DEFAULT_MODE", "")
    return env.lower() if env.lower() in RUNTIME_MODES else DEFAULT_MODE


def filter_skill_body_for_mode(body: str, mode) -> str:
    """Line-for-line port of filterSkillBodyForMode() in hooks/ponytail-instructions.js."""
    effective = normalize_mode(mode) or DEFAULT_MODE
    without_frontmatter = _JS_FRONTMATTER.sub("", str(body or ""), count=1)
    kept = []
    for line in re.split(r"\r?\n", without_frontmatter):
        table = _TABLE_LABEL.match(line)
        if table:
            label_mode = normalize_mode(table.group(1).strip())
            if label_mode:
                if label_mode == effective:
                    kept.append(line)
                continue
        example = _EXAMPLE_LABEL.match(line)
        if example:
            label_mode = normalize_mode(example.group(1).strip())
            if label_mode:
                if label_mode == effective:
                    kept.append(line)
                continue
        kept.append(line)
    return "\n".join(kept)


def get_ponytail_instructions(mode) -> str:
    """Port of getPonytailInstructions() (hooks/ponytail-instructions.js)."""
    configured = _normalize_persisted_mode(mode) or DEFAULT_MODE
    if configured == "review":
        return f"PONYTAIL MODE ACTIVE — level: {configured}. Behavior defined by /ponytail-{configured} skill."
    effective = normalize_mode(configured) or DEFAULT_MODE
    body = (SKILLS_DIR / "ponytail" / "SKILL.md").read_text(encoding="utf-8")
    return f"PONYTAIL MODE ACTIVE — level: {effective}\n\n" + filter_skill_body_for_mode(body, effective)


def resolve_mode(requested) -> str:
    """Port of resolveMode() in ponytail-mcp/instructions.js."""
    asked = normalize_mode(requested)
    if asked and asked != "off":
        return asked
    fallback = normalize_mode(get_default_mode())
    return fallback if fallback and fallback != "off" else "full"


def build_instructions(requested) -> str:
    return get_ponytail_instructions(resolve_mode(requested))


def companion_skill(name: str) -> tuple[str, str]:
    """(description, body) of one of the companion skills."""
    if name not in COMPANION_SKILLS:
        raise ValueError(f"unknown ponytail skill {name!r}; choose one of {', '.join(COMPANION_SKILLS)}")
    meta, body = split_frontmatter((SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8"))
    return " ".join(str(meta.get("description", "")).split()), body


def _skill_prompt(body: str):
    def render(args: str = "") -> str:
        """args: optional arguments, as typed after the slash command."""
        return body + (f"\n\nArguments: {args}" if args.strip() else "")
    return render


def _file_reader(path):
    def read() -> str:
        return path.read_text(encoding="utf-8")
    return read


class PonytailInstructions(BaseModel):
    mode: str
    instructions: str


def build_server(**settings) -> FastMCP:
    mcp = FastMCP(
        "ponytail",
        instructions=build_instructions(None),
        website_url="https://github.com/DietrichGebert/ponytail",
        **settings,
    )

    @mcp.prompt(
        name="ponytail",
        title="Ponytail mode",
        description="Lazy senior dev instructions: YAGNI, stdlib first, the smallest correct change.",
    )
    def ponytail_prompt(mode: str = "") -> str:
        """mode: Ponytail intensity: lite, full, or ultra. Omit for the configured default."""
        return build_instructions(mode or None)

    @mcp.tool(
        name="ponytail_instructions",
        title="Ponytail instructions",
        description="Return the Ponytail ruleset for the given intensity (lite, full, or ultra).",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    def ponytail_instructions(mode: str = "") -> Annotated[CallToolResult, PonytailInstructions]:
        resolved = resolve_mode(mode or None)
        text = build_instructions(resolved)
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent={"mode": resolved, "instructions": text},
        )

    @mcp.tool(
        name="ponytail_skill",
        title="Ponytail companion skill",
        description=(
            "Return the full instructions of a Ponytail companion skill, then follow them: "
            "ponytail-review (over-engineering review of a diff), ponytail-audit (whole-repo audit, "
            "ranked list of what to delete), ponytail-debt (collect `ponytail:` shortcut markers), "
            "ponytail-gain (benchmark scoreboard), ponytail-help (reference card)."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    def ponytail_skill(name: str) -> str:
        return companion_skill(name)[1]

    for skill in COMPANION_SKILLS:
        description, body = companion_skill(skill)
        mcp.add_prompt(Prompt.from_function(_skill_prompt(body), name=skill, title=skill, description=description))

    for name in ("ponytail",) + COMPANION_SKILLS:
        mcp.resource(
            f"ponytail://skills/{name}", name=name, mime_type="text/markdown",
            description=f"Upstream SKILL.md of {name}",
        )(_file_reader(SKILLS_DIR / name / "SKILL.md"))

    return mcp
