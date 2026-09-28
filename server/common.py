"""Small helpers shared by the MCP endpoints."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor"

_FRONTMATTER = re.compile(r"^---\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.S)


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body) for a Markdown file with optional YAML frontmatter."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), text[m.end():].lstrip("\n")


def safe_join(base: Path, rel: str) -> Path:
    """Resolve rel under base, refusing anything that escapes it (../, absolute paths, symlinks)."""
    target = (base / rel).resolve()
    if target != base.resolve() and base.resolve() not in target.parents:
        raise ValueError(f"path escapes the allowed directory: {rel}")
    return target
