"""Addy Osmani's agent-skills as a remote MCP server.

Upstream (addyosmani/agent-skills) ships no MCP server: it is a Claude Code plugin made of
25 skills, 9 slash commands, 4 agent personas and 7 reference checklists. This endpoint
serves that exact vendored content and reproduces how a plugin host uses it:

- skill discovery: the server `instructions` carry each skill's name + description (what a
  plugin host keeps in context) plus the discovery rules of `using-agent-skills` (what the
  upstream session-start hook injects on hosts without native skill routing);
- progressive disclosure: `get_skill` loads a full SKILL.md on demand, `read_file` loads
  its supporting files and the shared references;
- slash commands: the 9 lifecycle commands (/spec … /ship) are MCP prompts; "Invoke the
  agent-skills:X skill" is honoured by appending that skill's full text, which is what
  invoking a skill does in Claude Code;
- personas: the 4 agents are prompts too (and readable through `get_agent`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts.base import Prompt
from mcp.types import ToolAnnotations

from .common import VENDOR, safe_join, split_frontmatter

AS_DIR = VENDOR / "agent-skills"
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_SKILL_REF = re.compile(r"agent-skills:([a-z0-9-]+)")


@dataclass(frozen=True)
class Doc:
    name: str
    description: str
    body: str
    path: Path


def _load_dir(kind: str) -> dict[str, Doc]:
    docs = {}
    if kind == "skills":
        files = sorted((AS_DIR / "skills").glob("*/SKILL.md"))
    else:
        files = sorted((AS_DIR / kind).glob("*.md"))
    for f in files:
        meta, body = split_frontmatter(f.read_text(encoding="utf-8"))
        name = str(meta.get("name") or (f.parent.name if kind == "skills" else f.stem))
        docs[name] = Doc(name, " ".join(str(meta.get("description", "")).split()), body, f)
    return docs


SKILLS = _load_dir("skills")
AGENTS = _load_dir("agents")
COMMANDS = _load_dir("commands")
REFERENCES = _load_dir("references")


def _pick(table: dict[str, Doc], name: str, kind: str) -> Doc:
    key = name.strip().removeprefix("agent-skills:").removeprefix("/")
    if key not in table:
        raise ValueError(f"unknown {kind} {name!r}. Available: {', '.join(table)}")
    return table[key]


def skill_files(doc: Doc) -> list[str]:
    root = doc.path.parent
    return sorted(str(p.relative_to(AS_DIR)) for p in root.rglob("*") if p.is_file() and p.name != "SKILL.md")


def render_skill(name: str) -> str:
    doc = _pick(SKILLS, name, "skill")
    extra = skill_files(doc)
    out = f"# Skill: {doc.name}\n\n{doc.body}"
    if extra:
        out += "\n\n---\nSupporting files (load with read_file): " + ", ".join(extra)
    return out


def render_command(name: str, arguments: str = "") -> str:
    """A slash command as Claude Code would expand it, with the skills it invokes appended."""
    doc = _pick(COMMANDS, name, "command")
    text = doc.body.replace("$ARGUMENTS", arguments.strip())
    if arguments.strip() and "$ARGUMENTS" not in doc.body:
        text += f"\n\nUser input: {arguments.strip()}"
    invoked = [s for s in dict.fromkeys(_SKILL_REF.findall(doc.body)) if s in SKILLS]
    for s in invoked:
        text += f"\n\n---\n{render_skill(s)}"
    return text


def search(query: str, limit: int = 5) -> list[tuple[int, Doc]]:
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
    scored = []
    for doc in SKILLS.values():
        head = f"{doc.name} {doc.description}".lower()
        body = doc.body.lower()
        score = sum(5 * head.count(w) + min(body.count(w), 10) for w in words)
        if score:
            scored.append((score, doc))
    return sorted(scored, key=lambda x: -x[0])[:limit]


def server_instructions() -> str:
    index = "\n".join(f"- {d.name}: {d.description}" for d in SKILLS.values())
    commands = ", ".join(f"/{c}" for c in COMMANDS)
    agents = ", ".join(AGENTS)
    return (
        "agent-skills loaded (addyosmani/agent-skills): production-grade engineering workflows "
        "for the whole lifecycle DEFINE → PLAN → BUILD → VERIFY → REVIEW → SHIP.\n\n"
        "How to use them here: before any non-trivial engineering task, pick the matching skill "
        "from the index below and call `get_skill(name)`, then follow the loaded workflow step by "
        "step (it replaces your default approach; do not paraphrase it from memory). If several "
        "apply, load the process skill first (spec, plan, tdd, debugging), then the domain skill. "
        "`get_skill('using-agent-skills')` holds the full discovery flowchart. Supporting "
        "checklists load with `read_file`.\n\n"
        f"Lifecycle commands are available as prompts: {commands}. Specialist personas: {agents}.\n\n"
        f"Skill index:\n{index}"
    )


def build_server(**settings) -> FastMCP:
    mcp = FastMCP(
        "agent-skills",
        instructions=server_instructions(),
        website_url="https://github.com/addyosmani/agent-skills",
        **settings,
    )

    @mcp.tool(title="List skills", description="List all agent-skills with their trigger descriptions.",
              annotations=READ_ONLY)
    def list_skills() -> str:
        return "\n".join(f"- {d.name}: {d.description}" for d in SKILLS.values())

    @mcp.tool(title="Load a skill",
              description="Load the full workflow of one skill (SKILL.md) so you can follow it. "
                          "Call this before starting the task the skill covers.",
              annotations=READ_ONLY)
    def get_skill(name: str) -> str:
        return render_skill(name)

    @mcp.tool(title="Search skills",
              description="Find the skills most relevant to a task description (keyword ranking).",
              annotations=READ_ONLY)
    def search_skills(query: str, limit: int = 5) -> str:
        hits = search(query, max(1, min(limit, 25)))
        if not hits:
            return "No skill matched. Call list_skills to browse all of them."
        return "\n".join(f"- {d.name} (score {s}): {d.description}" for s, d in hits)

    @mcp.tool(title="Read a supporting file",
              description="Read a supporting file of the collection by its relative path, e.g. "
                          "'references/security-checklist.md' or "
                          "'skills/idea-refine/frameworks.md'. Paths are listed by get_skill.",
              annotations=READ_ONLY)
    def read_file(path: str) -> str:
        rel = path.strip().lstrip("/")
        if not rel.startswith(("skills/", "references/", "agents/", "commands/")):
            rel = f"references/{rel}" if (AS_DIR / "references" / rel).is_file() else rel
        target = safe_join(AS_DIR, rel)
        if not target.is_file():
            raise ValueError(f"no such file: {path}")
        return target.read_text(encoding="utf-8")

    @mcp.tool(title="List references",
              description="List the shared reference checklists (security, performance, "
                          "accessibility, testing, observability, definition of done, orchestration).",
              annotations=READ_ONLY)
    def list_references() -> str:
        return "\n".join(f"- references/{d.path.name}: {d.body.splitlines()[0].lstrip('# ') if d.body else ''}"
                         for d in REFERENCES.values())

    @mcp.tool(title="Load an agent persona",
              description="Load a specialist persona (code-reviewer, security-auditor, test-engineer, "
                          "web-performance-auditor) and adopt it for the current review.",
              annotations=READ_ONLY)
    def get_agent(name: str) -> str:
        doc = _pick(AGENTS, name, "agent")
        return f"# Agent: {doc.name}\n{doc.description}\n\n{doc.body}"

    @mcp.tool(title="Run a lifecycle command",
              description="Expand one of the lifecycle commands (spec, plan, build, test, constraints, "
                          "review, webperf, code-simplify, ship) with the skills it invokes, then follow "
                          "it. Same text as the slash command.",
              annotations=READ_ONLY)
    def run_command(name: str, arguments: str = "") -> str:
        return render_command(name, arguments)

    for name, doc in COMMANDS.items():
        mcp.add_prompt(Prompt.from_function(_command_prompt(name), name=name, title=f"/{name}",
                                            description=doc.description))
    for name, doc in AGENTS.items():
        mcp.add_prompt(Prompt.from_function(_agent_prompt(doc), name=f"agent-{name}",
                                            title=f"Agent: {name}", description=doc.description))
    for name, doc in SKILLS.items():
        mcp.resource(f"agent-skills://skills/{name}", name=name, description=doc.description,
                     mime_type="text/markdown")(_reader(doc.path))
    for name, doc in REFERENCES.items():
        mcp.resource(f"agent-skills://references/{name}", name=f"reference: {name}",
                     mime_type="text/markdown")(_reader(doc.path))
    return mcp


def _command_prompt(name: str):
    def render(arguments: str = "") -> str:
        """arguments: optional text typed after the command (e.g. 'auto' for /build)."""
        return render_command(name, arguments)
    return render


def _agent_prompt(doc: Doc):
    def render(target: str = "") -> str:
        """target: what to review or audit (files, PR, feature)."""
        return doc.body + (f"\n\nTarget: {target}" if target.strip() else "")
    return render


def _reader(path: Path):
    def read() -> str:
        return path.read_text(encoding="utf-8")
    return read
