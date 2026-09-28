"""Graphify as a remote MCP server.

Upstream graphify (Graphify-Labs/graphify, PyPI `graphifyy`) already ships an MCP server
(`python -m graphify.serve`) with a Streamable HTTP transport, but it serves graph.json
files that live on the machine running it. A connector used from claude.ai has no local
checkout, so this endpoint adds the missing half on the server side:

1. `index_repository` clones a git repository and runs the original graphify pipeline on
   it (`graphify extract --code-only` then `graphify cluster-only`: tree-sitter AST,
   deterministic, no LLM, exactly what `/graphify` does for code);
2. every original query tool (query_graph, get_node, get_neighbors, get_community,
   god_nodes, graph_stats, shortest_path, list_prs, get_pr_impact, triage_prs) and every
   original resource (report, stats, god nodes, surprises, audit, questions) is delegated
   untouched to graphify's own MCP server instance for that repository's graph.

Only the graph selection changes: tools take a `graph` id (from index_repository or
list_graphs) instead of a server-local `project_path`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import sys
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

from mcp import types
from mcp.server.lowlevel import Server

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "graphify"
MAX_GRAPHS = int(os.environ.get("GRAPHIFY_MAX_GRAPHS", "25"))
MAX_REPO_MB = int(os.environ.get("GRAPHIFY_MAX_REPO_MB", "1024"))
ALLOWED_HOSTS = {
    h.strip().lower()
    for h in os.environ.get(
        "GRAPHIFY_ALLOWED_HOSTS", "github.com,gitlab.com,bitbucket.org,codeberg.org,git.sr.ht"
    ).split(",")
    if h.strip()
}
CLONE_TIMEOUT = 300
EXTRACT_TIMEOUT = 1800

INSTRUCTIONS = """graphify: turns a code repository into a knowledge graph you query instead of grepping.

Workflow:
1. `index_repository(url)` once per repository (public GitHub/GitLab/… URL or owner/repo). Code is parsed with tree-sitter AST across ~40 languages: deterministic, no LLM. Re-run with force=true after the repository changes.
2. For codebase questions, first call `query_graph` (scoped subgraph for a plain-language question). Use `shortest_path` for how two things connect and `get_node` / `get_neighbors` for one concept. These return a scoped subgraph, usually much smaller than the full report or raw file dumps.
3. `graph_insights(kind="report")` (GRAPH_REPORT.md) only for broad architecture review, or when query/path/node do not surface enough context. Other kinds: god-nodes, surprises, questions, audit, stats.

Every edge is tagged EXTRACTED (explicit in the source) or INFERRED (resolved by graphify); say which when it matters."""

_locks: dict[str, asyncio.Lock] = {}
_index_slots = asyncio.Semaphore(2)
_servers: "OrderedDict[str, Server]" = OrderedDict()
_MAX_LIVE_SERVERS = 4


# ---------------------------------------------------------------- repository ids

def parse_repo(spec: str) -> tuple[str, str, str]:
    """Return (clone_url, graph_id, owner/repo or '') for a URL or owner/repo shorthand."""
    spec = spec.strip()
    if re.fullmatch(r"[\w.-]+/[\w.-]+", spec):
        spec = f"https://github.com/{spec}"
    u = urlparse(spec)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host:
        raise ValueError("repository must be an https URL or owner/repo")
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"host {host} is not allowed (allowed: {', '.join(sorted(ALLOWED_HOSTS))})")
    if u.username or u.password or u.port or u.query or u.fragment:
        raise ValueError("credentials, ports, query strings and fragments are not accepted in the URL")
    parts = [p for p in u.path.split("/") if p]
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][:-4]
    if host == "github.com" and len(parts) >= 4 and parts[2] == "tree":
        parts = parts[:2]  # https://github.com/o/r/tree/branch -> o/r (ref comes from the `ref` arg)
    if len(parts) < 2 or any(not re.fullmatch(r"[\w.-]+", p) or p in (".", "..") for p in parts):
        raise ValueError("could not read owner/repository from the URL")
    path = "/".join(parts)
    graph_id = path if host == "github.com" else f"{host}/{path}"
    return f"https://{host}/{path}.git", graph_id, (path if host == "github.com" and len(parts) == 2 else "")


def _graph_dir(graph_id: str) -> Path:
    return DATA_DIR / "graphs" / re.sub(r"[^\w.@-]", "__", graph_id)


def _meta(graph_id: str) -> dict | None:
    try:
        return json.loads((_graph_dir(graph_id) / "meta.json").read_text())
    except (OSError, ValueError):
        return None


def _all_meta() -> list[dict]:
    root = DATA_DIR / "graphs"
    out = []
    for d in sorted(root.glob("*")) if root.exists() else []:
        try:
            out.append(json.loads((d / "meta.json").read_text()))
        except (OSError, ValueError):
            continue
    return out


def _resolve_graph(graph: str) -> tuple[str, dict]:
    """Map a user-supplied graph id or URL to an indexed graph."""
    candidates = [graph.strip()]
    try:
        candidates.append(parse_repo(graph)[1])
    except ValueError:
        pass
    for gid in candidates:
        m = _meta(gid)
        if m and (_graph_dir(gid) / "graphify-out" / "graph.json").exists():
            return gid, m
    known = ", ".join(m["graph"] for m in _all_meta()) or "none yet"
    raise ValueError(f"graph {graph!r} is not indexed. Call index_repository first. Indexed: {known}")


# ---------------------------------------------------------------- indexing

async def _run(*cmd: str, cwd: Path | None = None, timeout: int, env: dict | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{cmd[0]} timed out after {timeout}s")
    text = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed ({proc.returncode}):\n{text[-2000:]}")
    return text


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN", "GIT_ASKPASS")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    return env


def _git_auth_args(clone_url: str) -> list[str]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token or not clone_url.startswith("https://github.com/"):
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]


def _dir_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink()) / 1e6


def _evict_old(keep: str) -> None:
    metas = sorted(_all_meta(), key=lambda m: m.get("last_used", 0))
    while len(metas) >= MAX_GRAPHS:
        victim = metas.pop(0)
        if victim["graph"] != keep:
            _drop(victim["graph"])


def _drop(graph_id: str) -> None:
    _servers.pop(graph_id, None)
    shutil.rmtree(_graph_dir(graph_id), ignore_errors=True)


_jobs: dict[str, asyncio.Task] = {}
WAIT_SECONDS = float(os.environ.get("GRAPHIFY_WAIT_SECONDS", "50"))


async def index_repository(url: str, ref: str = "", force: bool = False) -> str:
    """Start (or join) an indexing job; answer within WAIT_SECONDS so the client never times out."""
    clone_url, graph_id, gh_repo = parse_repo(url)
    if ref and (not re.fullmatch(r"[\w./-]{1,200}", ref) or ref.startswith("-") or ".." in ref):
        raise ValueError("invalid ref")
    job = _jobs.get(graph_id)
    if job is None or job.done():
        job = asyncio.create_task(_index(clone_url, graph_id, gh_repo, ref, force))
        _jobs[graph_id] = job
    try:
        return await asyncio.wait_for(asyncio.shield(job), WAIT_SECONDS)
    except asyncio.TimeoutError:
        return (f"Indexing {graph_id} is still running in the background (large repository). "
                f"Call index_repository with the same url again in a minute to get the result.")


async def _index(clone_url: str, graph_id: str, gh_repo: str, ref: str, force: bool) -> str:
    lock = _locks.setdefault(graph_id, asyncio.Lock())
    async with lock:
        existing = _meta(graph_id)
        if existing and not force and existing.get("ref", "") == ref:
            return f"Already indexed (use force=true to rebuild).\n\n{_summary(graph_id)}"
        async with _index_slots:
            _evict_old(keep=graph_id)
            _drop(graph_id)
            gdir = _graph_dir(graph_id)
            src = gdir / "src"
            src.mkdir(parents=True)
            started = time.time()
            env = _git_env()
            git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
                   "-c", "protocol.https.allow=always", *_git_auth_args(clone_url)]
            try:
                await _run("git", "init", "-q", str(src), timeout=30, env=env)
                await _run(*git, "-C", str(src), "fetch", "-q", "--depth", "1", "--no-tags", clone_url,
                           ref or "HEAD", timeout=CLONE_TIMEOUT, env=env)
                await _run(*git, "-C", str(src), "-c", "advice.detachedHead=false", "checkout", "-q",
                           "FETCH_HEAD", timeout=CLONE_TIMEOUT, env=env)
                commit = (await _run("git", "-C", str(src), "rev-parse", "HEAD", timeout=30, env=env)).strip()
                size = _dir_mb(src)
                if size > MAX_REPO_MB:
                    raise RuntimeError(f"repository is {size:.0f} MB, above GRAPHIFY_MAX_REPO_MB={MAX_REPO_MB}")
                graphify = [sys.executable, "-m", "graphify"]
                genv = dict(os.environ, GRAPHIFY_NO_TIPS="1")
                log = await _run(*graphify, "extract", str(src), "--code-only", "--out", str(gdir),
                                 timeout=EXTRACT_TIMEOUT, env=genv)
                log += await _run(*graphify, "cluster-only", str(gdir), timeout=EXTRACT_TIMEOUT, env=genv)
            except Exception:
                _drop(graph_id)
                raise
            meta = {
                "graph": graph_id, "url": clone_url, "ref": ref, "commit": commit, "github_repo": gh_repo,
                "indexed_at": int(time.time()), "last_used": int(time.time()),
                "seconds": round(time.time() - started, 1), "repo_mb": round(size, 1),
            }
            (gdir / "meta.json").write_text(json.dumps(meta, indent=2))
            tail = "\n".join(l for l in log.splitlines() if l.startswith("[graphify extract] found") or "wrote" in l)
            return f"Indexed {graph_id} @ {commit[:12]} in {meta['seconds']}s.\n{tail}\n\n{_summary(graph_id)}"


def _summary(graph_id: str) -> str:
    report = _graph_dir(graph_id) / "graphify-out" / "GRAPH_REPORT.md"
    text = report.read_text(encoding="utf-8") if report.exists() else ""
    head = text.split("\n## Community Hubs")[0].strip()
    return f"graph id: {graph_id}\n\n{head}\n\nNext: query_graph(graph=\"{graph_id}\", question=...)"


# ---------------------------------------------------------------- delegation to graphify.serve

def _graphify_server(graph_id: str) -> Server:
    from graphify.serve import _build_server

    if graph_id in _servers:
        _servers.move_to_end(graph_id)
        return _servers[graph_id]
    srv = _build_server(str(_graph_dir(graph_id) / "graphify-out" / "graph.json"))
    _servers[graph_id] = srv
    while len(_servers) > _MAX_LIVE_SERVERS:
        _servers.popitem(last=False)
    return srv


def _touch(graph_id: str, meta: dict) -> None:
    meta["last_used"] = int(time.time())
    try:
        (_graph_dir(graph_id) / "meta.json").write_text(json.dumps(meta, indent=2))
    except OSError:
        pass


async def _delegate_tool(graph_id: str, name: str, arguments: dict) -> types.CallToolResult:
    srv = _graphify_server(graph_id)
    handler = srv.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name=name, arguments=arguments))
    return (await handler(req)).root


async def _delegate_resource(graph_id: str, uri: str) -> str:
    srv = _graphify_server(graph_id)
    handler = srv.request_handlers[types.ReadResourceRequest]
    req = types.ReadResourceRequest(method="resources/read", params=types.ReadResourceRequestParams(uri=uri))
    result = (await handler(req)).root
    return "\n".join(getattr(c, "text", "") for c in result.contents)


_UPSTREAM_TOOLS: list[types.Tool] | None = None


async def upstream_tools() -> list[types.Tool]:
    """graphify's own tool list, with project_path swapped for our `graph` selector."""
    global _UPSTREAM_TOOLS
    if _UPSTREAM_TOOLS is None:
        from graphify.serve import _build_server

        probe = _build_server(str(DATA_DIR / "__none__" / "graph.json"))
        listed = (await probe.request_handlers[types.ListToolsRequest](types.ListToolsRequest(method="tools/list"))).root
        tools = []
        for t in listed.tools:
            schema = json.loads(json.dumps(t.inputSchema))
            props = schema.setdefault("properties", {})
            props.pop("project_path", None)
            props["graph"] = {"type": "string",
                              "description": "Graph id from index_repository / list_graphs (e.g. owner/repo)."}
            schema["required"] = ["graph", *[r for r in schema.get("required", []) if r != "project_path"]]
            tools.append(t.model_copy(update={"inputSchema": schema}))
        _UPSTREAM_TOOLS = tools
    return _UPSTREAM_TOOLS


RESOURCE_KINDS = {"report": "graphify://report", "stats": "graphify://stats", "god-nodes": "graphify://god-nodes",
                  "surprises": "graphify://surprises", "audit": "graphify://audit",
                  "questions": "graphify://questions"}

OWN_TOOLS = [
    types.Tool(
        name="index_repository",
        title="Index a repository",
        description=("Clone a git repository and build its graphify knowledge graph (tree-sitter AST, "
                     "deterministic, no LLM). Accepts an https URL or GitHub owner/repo. Returns the graph "
                     "id and the report summary. Takes seconds to a few minutes on large repositories."),
        inputSchema={"type": "object", "properties": {
            "url": {"type": "string", "description": "https://github.com/owner/repo, owner/repo, or another allowed git host URL"},
            "ref": {"type": "string", "description": "Branch, tag or commit SHA. Default: the default branch."},
            "force": {"type": "boolean", "default": False, "description": "Rebuild even if already indexed."},
        }, "required": ["url"]},
    ),
    types.Tool(
        name="list_graphs",
        title="List indexed graphs",
        description="List repositories already indexed on this server, with commit and size.",
        inputSchema={"type": "object", "properties": {}},
        annotations=types.ToolAnnotations(readOnlyHint=True),
    ),
    types.Tool(
        name="graph_insights",
        title="Graph report and insights",
        description=("Read graphify's analysis of a graph: report (full GRAPH_REPORT.md), god-nodes, surprises "
                     "(cross-community connections), questions (suggested questions), audit "
                     "(EXTRACTED/INFERRED/AMBIGUOUS breakdown), stats."),
        inputSchema={"type": "object", "properties": {
            "graph": {"type": "string", "description": "Graph id from index_repository / list_graphs."},
            "kind": {"type": "string", "enum": list(RESOURCE_KINDS), "default": "report"},
        }, "required": ["graph"]},
        annotations=types.ToolAnnotations(readOnlyHint=True),
    ),
    types.Tool(
        name="remove_graph",
        title="Remove an indexed graph",
        description="Delete an indexed repository and its graph from this server.",
        inputSchema={"type": "object", "properties": {"graph": {"type": "string"}}, "required": ["graph"]},
        annotations=types.ToolAnnotations(destructiveHint=True),
    ),
]


def _text(text: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=error)


async def call(name: str, arguments: dict) -> types.CallToolResult:
    args = dict(arguments or {})
    try:
        if name == "index_repository":
            return _text(await index_repository(str(args.get("url", "")), str(args.get("ref") or ""),
                                                bool(args.get("force", False))))
        if name == "list_graphs":
            metas = _all_meta()
            if not metas:
                return _text("No repository indexed yet. Call index_repository(url).")
            return _text("\n".join(
                f"- {m['graph']} @ {m['commit'][:12]}{' (' + m['ref'] + ')' if m.get('ref') else ''} — "
                f"{m.get('repo_mb', '?')} MB, indexed {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(m['indexed_at']))}"
                for m in metas))
        graph_id, meta = _resolve_graph(str(args.pop("graph", "") or ""))
        if name == "remove_graph":
            _drop(graph_id)
            return _text(f"Removed {graph_id}.")
        _touch(graph_id, meta)
        if name == "graph_insights":
            kind = str(args.get("kind") or "report")
            if kind not in RESOURCE_KINDS:
                raise ValueError(f"kind must be one of {', '.join(RESOURCE_KINDS)}")
            return _text(await _delegate_resource(graph_id, RESOURCE_KINDS[kind]))
        if name not in {t.name for t in await upstream_tools()}:
            raise ValueError(f"unknown tool {name}")
        if name in ("list_prs", "get_pr_impact", "triage_prs") and not args.get("repo"):
            if not meta.get("github_repo"):
                raise ValueError("PR tools need a GitHub repository; pass repo=owner/repo")
            args["repo"] = meta["github_repo"]
        return await _delegate_tool(graph_id, name, args)
    except Exception as exc:  # surfaced to the model as a tool error, never a transport error
        return _text(f"Error: {exc}", error=True)


def build_server() -> Server:
    server = Server("graphify", instructions=INSTRUCTIONS, website_url="https://github.com/Graphify-Labs/graphify")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return OWN_TOOLS + await upstream_tools()

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        return await call(name, arguments)

    return server
