#!/usr/bin/env bash
# Refresh vendor/ from upstream HEAD, then rebuild the claude.ai skill zips.
set -euo pipefail
cd "$(dirname "$0")/.."
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
git clone -q --depth 1 https://github.com/DietrichGebert/ponytail "$tmp/ponytail"
git clone -q --depth 1 https://github.com/addyosmani/agent-skills "$tmp/agent-skills"
rm -rf vendor/ponytail vendor/agent-skills
mkdir -p vendor/ponytail/hooks vendor/agent-skills/commands
cp -r "$tmp"/ponytail/{skills,commands,AGENTS.md,LICENSE} vendor/ponytail/
cp "$tmp"/ponytail/hooks/{ponytail-instructions.js,ponytail-config.js} vendor/ponytail/hooks/
cp -r "$tmp"/agent-skills/{skills,agents,references,LICENSE,AGENTS.md} vendor/agent-skills/
cp "$tmp"/agent-skills/.claude/commands/*.md vendor/agent-skills/commands/
sed -i "s/^\(ponytail .*github.com\/DietrichGebert\/ponytail *\)[0-9a-f]*/\1$(git -C "$tmp/ponytail" rev-parse HEAD)/;
        s/^\(agent-skills .*github.com\/addyosmani\/agent-skills *\)[0-9a-f]*/\1$(git -C "$tmp/agent-skills" rev-parse HEAD)/" vendor/UPSTREAM.txt
python scripts/build_claude_skills.py >/dev/null
echo "vendor/ refreshed; run: python -m pytest -q tests"
