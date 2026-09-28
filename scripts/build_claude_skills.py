"""Package the vendored Ponytail and agent-skills skills as claude.ai Skill uploads.

    python scripts/build_claude_skills.py      # writes claude-skills/<collection>/<skill>.zip

claude.ai (Settings > Capabilities > Skills > Upload skill) takes one zip per skill with a
top-level folder holding SKILL.md. Content is copied verbatim; the only change is that
agent-skills' shared checklists (repo-level references/) are copied into each skill that
cites them and the relative links are rewritten to point there, as upstream README
recommends for single-skill installs.
"""

import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor"
OUT = ROOT / "claude-skills"
SHARED_REF = re.compile(r"(?:\.\./)+references/([a-z0-9-]+\.md)")


def build(collection: str) -> list[Path]:
    src_root = VENDOR / collection
    shared = src_root / "references"
    out_dir = OUT / collection
    out_dir.mkdir(parents=True, exist_ok=True)
    built = []
    for skill_dir in sorted(p for p in (src_root / "skills").iterdir() if (p / "SKILL.md").exists()):
        target = out_dir / f"{skill_dir.name}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            needed = set()
            for f in sorted(p for p in skill_dir.rglob("*") if p.is_file()):
                rel = f.relative_to(skill_dir)
                data = f.read_bytes()
                if f.suffix == ".md" and shared.exists():
                    text = data.decode("utf-8")
                    depth = len(rel.parts) - 1
                    needed |= set(SHARED_REF.findall(text))
                    text = SHARED_REF.sub(lambda m: "../" * depth + "references/" + m.group(1), text)
                    data = text.encode("utf-8")
                z.writestr(f"{skill_dir.name}/{rel.as_posix()}", data)
            for name in sorted(needed):
                if (shared / name).exists():
                    z.write(shared / name, f"{skill_dir.name}/references/{name}")
            z.write(src_root / "LICENSE", f"{skill_dir.name}/LICENSE")
        built.append(target)
    return built


if __name__ == "__main__":
    shutil.rmtree(OUT, ignore_errors=True)
    for collection in ("ponytail", "agent-skills"):
        for path in build(collection):
            print(path.relative_to(ROOT))
