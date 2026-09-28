"""End-to-end check of the four connectors with the official MCP client (Streamable HTTP).

    python scripts/smoke_test.py https://<host>/<MCP_SECRET> [owner/repo-to-index]

Exits non-zero on the first failure.
"""

import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def session(url, fn):
    async with streamablehttp_client(url, timeout=60, sse_read_timeout=600) as (r, w, _):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            return await fn(s, init)


def text(result):
    assert not result.isError, result.content[0].text
    return result.content[0].text


async def ponytail(s, init):
    assert "PONYTAIL MODE ACTIVE — level: full" in (init.instructions or "")
    tools = [t.name for t in (await s.list_tools()).tools]
    prompts = [p.name for p in (await s.list_prompts()).prompts]
    ultra = await s.call_tool("ponytail_instructions", {"mode": "ultra"})
    assert ultra.structuredContent["mode"] == "ultra" and "**ultra**" in text(ultra) and "**lite**" not in text(ultra)
    review = await s.get_prompt("ponytail-review", {"args": "src/"})
    assert "Arguments: src/" in review.messages[0].content.text
    return f"ponytail ok: tools={tools} prompts={prompts}"


async def skills(s, init):
    assert "test-driven-development" in (init.instructions or "")
    tools = [t.name for t in (await s.list_tools()).tools]
    prompts = [p.name for p in (await s.list_prompts()).prompts]
    tdd = text(await s.call_tool("get_skill", {"name": "test-driven-development"}))
    assert tdd.startswith("# Skill: test-driven-development")
    build = await s.get_prompt("build", {"arguments": "auto"})
    assert "# Skill: incremental-implementation" in build.messages[0].content.text
    hits = text(await s.call_tool("search_skills", {"query": "security review of auth"}))
    ref = text(await s.call_tool("read_file", {"path": "references/security-checklist.md"}))
    return f"agent-skills ok: {len(tools)} tools, {len(prompts)} prompts, search→ {hits.splitlines()[0]}, ref {len(ref)} chars"


async def graphify(s, init, repo):
    tools = [t.name for t in (await s.list_tools()).tools]
    idx = await s.call_tool("index_repository", {"url": repo})
    out = text(idx)
    while "still running" in out:
        out = text(await s.call_tool("index_repository", {"url": repo}))
    gid = out.split("graph id: ")[1].split()[0]
    q = text(await s.call_tool("query_graph", {"graph": gid, "question": "how is the mode configured"}))
    gods = text(await s.call_tool("god_nodes", {"graph": gid}))
    rep = text(await s.call_tool("graph_insights", {"graph": gid, "kind": "questions"}))
    return f"graphify ok: {len(tools)} tools, graph {gid}\n{out[:400]}\n--- query ---\n{q[:500]}\n--- god nodes ---\n{gods[:300]}\n--- questions ---\n{rep[:300]}"


async def omniroute(s, init):
    tools = [t.name for t in (await s.list_tools()).tools]
    health = text(await s.call_tool("omniroute_get_health", {}))
    return f"omniroute ok: {len(tools)} tools, health: {health[:120]}"


async def main(base, repo):
    print(await session(f"{base}/ponytail/mcp", ponytail))
    print(await session(f"{base}/agent-skills/mcp", skills))
    print(await session(f"{base}/graphify/mcp", lambda s, i: graphify(s, i, repo)))
    print(await session(f"{base}/omniroute/mcp", omniroute))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1].rstrip("/"), sys.argv[2] if len(sys.argv) > 2 else "DietrichGebert/ponytail"))
