"""Command line: run the MCP server, manage the config file, and use the tools from a shell.

reuse-layer mcp                       run the MCP server on stdio (what agent apps launch)
reuse-layer config init|path|show     create / locate / summarise the config file (keys masked)
reuse-layer keys set <name>           store a key in the OS keychain (prompted, never echoed)
reuse-layer doctor                    check the config and the GitHub connection
reuse-layer find "<task>" [...]       same as the find_reusable tool, JSON or text output
reuse-layer search "<query>" [...]    same as the search_github tool
reuse-layer install-skill [...]       install the discovery skill for Claude Code
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import shutil
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from reuse_layer import __version__
from reuse_layer.config import (
    KNOWN_KEYS,
    ConfigError,
    Settings,
    default_config_path,
    inside_git_repo,
    redaction_filter,
    store_in_keyring,
    write_template,
)

SKILL = "github-oss-discovery"
_WRITE_SCOPES = {
    "repo",
    "public_repo",
    "delete_repo",
    "workflow",
    "write:packages",
    "admin:org",
    "admin:repo_hook",
    "write:org",
    "admin:public_key",
    "admin:gpg_key",
    "user",
    "gist",
    "write:discussion",
}


def _setup_logging(verbose: int) -> None:
    level = logging.DEBUG if verbose >= 2 else logging.INFO if verbose else logging.WARNING
    requested_level = os.environ.get("REUSE_LAYER_LOG_LEVEL", "").upper()
    if requested_level:
        configured_level = logging.getLevelNamesMapping().get(requested_level)
        if isinstance(configured_level, int):
            level = configured_level
    handler = logging.StreamHandler(sys.stderr)  # stdout is the MCP protocol channel
    handler.addFilter(redaction_filter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    if requested_level and not isinstance(logging.getLevelNamesMapping().get(requested_level), int):
        logging.getLogger(__name__).warning(
            "ignoring invalid REUSE_LAYER_LOG_LEVEL=%r; use DEBUG, INFO, WARNING, ERROR or CRITICAL",
            requested_level,
        )


def _load(args: argparse.Namespace) -> Settings:
    path = Path(args.config).expanduser() if getattr(args, "config", None) else None
    settings = Settings.load(path)
    for w in settings.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return settings


# --------------------------------------------------------------------------- commands


def cmd_mcp(args: argparse.Namespace) -> int:
    from reuse_layer.adapters.mcp_server import run_stdio

    run_stdio(_load(args))
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    path = Path(args.config).expanduser() if args.config else default_config_path()
    if args.action == "path":
        print(path)
        return 0
    if args.action == "init":
        write_template(path)
        print(f"Created {path}")
        print("Open it and paste your GitHub token under [keys] (see the comments for how to create one).")
        print("Then run: reuse-layer doctor")
        return 0
    settings = _load(args)
    print(
        f"config: {settings.config_path}{'' if settings.config_path and settings.config_path.exists() else ' (missing)'}"
    )
    for name in KNOWN_KEYS:
        print(f"  {name:12} {'set' if settings.secret(name) else 'not set'}")
    print(f"storage: {settings.db_path()}")
    print(f"license policy: {settings.gates.license_policy}; min age {settings.gates.min_age_days} days")
    print(f"relevance judge: {settings.relevance.judge} ({settings.relevance.model})")
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    if args.name not in KNOWN_KEYS:
        print(f"unknown key {args.name!r}; known: {', '.join(KNOWN_KEYS)}", file=sys.stderr)
        return 2
    value = getpass.getpass(f"Paste the {args.name} key (input hidden): ").strip()
    if not value:
        print("nothing entered; no change", file=sys.stderr)
        return 1
    store_in_keyring("reuse-layer", args.name, value)
    print(f'Stored in the OS keychain. In your config set:  {args.name} = {{ keyring = "reuse-layer" }}')
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from reuse_layer.core.engine import Engine

    try:
        settings = _load(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    ok = True
    path = settings.config_path
    print(
        f"config file: {path} ({'found' if path and path.exists() else 'missing: run `reuse-layer config init`'})"
    )
    if path and path.exists() and inside_git_repo(path):
        print("  ! the config file is inside a git repository; move it outside your projects")
        ok = False
    print(f"storage: {settings.db_path()}")

    async def check() -> bool:
        import httpx

        engine = Engine.create(settings)
        good = True
        try:
            token = settings.secret("github")
            if token is None:
                print("github: not connected (60 requests/hour). Add keys.github to the config file.")
                good = False
            # A raw call so the token's scope header can be inspected; the token itself is never printed.
            async with httpx.AsyncClient(timeout=10) as http:
                headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
                if token is not None:
                    headers["Authorization"] = f"Bearer {token.reveal()}"
                resp = await http.get(f"{settings.github.api_url}/rate_limit", headers=headers)
            if resp.status_code == 401:
                print("github: the token was rejected (401). Create a new one and update the config file.")
                return False
            if resp.status_code != 200:
                print(f"github: unexpected HTTP {resp.status_code} from /rate_limit")
                return False
            core = resp.json().get("resources", {}).get("core", {})
            search = resp.json().get("resources", {}).get("search", {})
            print(
                f"github: reachable; core {core.get('remaining')}/{core.get('limit')}, search {search.get('remaining')}/{search.get('limit')} per window"
            )
            scopes = {s.strip() for s in resp.headers.get("x-oauth-scopes", "").split(",") if s.strip()}
            risky = sorted(scopes & _WRITE_SCOPES)
            if risky:
                print(
                    f"  ! this classic token has write/private scopes ({', '.join(risky)}). v1 needs none: create a "
                    "fine-grained token with public, read-only access."
                )
                good = False
            elif token is not None:
                print("  token scopes: none beyond public read (good)")
        finally:
            await engine.aclose()
        if settings.secret("anthropic"):
            print(
                f"relevance: {'LLM judge ' + settings.relevance.model if engine.llm else 'lexical (install reuse-layer[llm] to use the Anthropic key)'}"
            )
        else:
            print("relevance: lexical (optional: add keys.anthropic for the small-model judge)")
        print(f"ledger: {engine.store.stats()}")
        return good

    ok = asyncio.run(check()) and ok
    print("OK" if ok else "Some checks need attention (see above).")
    return 0 if ok else 1


def cmd_find(args: argparse.Namespace) -> int:
    from reuse_layer.core.engine import Engine
    from reuse_layer.core.models import CallerContext, FindRequest

    settings = _load(args)
    ctx = CallerContext(
        language=args.language,
        license_policy=args.license_policy,
        allow_new=args.allow_new,
        exclude=args.exclude or [],
    )
    req = FindRequest(
        task=args.task,
        component=args.component,
        capabilities=args.capability or [],
        candidates=args.candidate or [],
        search_queries=args.query or [],
        avoid=args.avoid or [],
        context=ctx,
    )

    async def go() -> dict[str, Any]:
        engine = Engine.create(settings)
        try:
            return await engine.find_reusable(req)
        finally:
            await engine.aclose()

    result = asyncio.run(go())
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_find(result)
    return 0


def _print_find(r: dict[str, Any]) -> None:
    rec = r.get("recommendation") or {}
    reason = rec.get("reason") or r.get("build_yourself", {}).get("reason", "")
    if r.get("component"):
        print(f"component: {r['component']}")
    print(f"verdict: {r['verdict']}  ({reason})")
    if rec.get("next"):
        print(f"next: {rec['next']}")
    cov = r.get("coverage") or {}
    if cov.get("unknown"):
        print(
            f"coverage: unknown for {len(cov['unknown'])} must-have capabilities (nothing could be assessed)"
        )
    elif cov.get("must_have"):
        print(f"coverage: {len(cov.get('covered', []))}/{cov['must_have']} must-have capabilities evidenced")
        for m in cov.get("missing", []):
            also = (cov.get("covered_by_other_picks") or {}).get(m)
            print(f"   - missing: {m}" + (f" (evidenced by {', '.join(also)})" if also else ""))
    evidence = r.get("evidence") or {}
    if evidence.get("status") not in (None, "complete"):
        print(f"evidence: {evidence['status']}")
    for c in r.get("unresolved_checks", []):
        print(f"   ? {c}")
    for i, p in enumerate(r["picks"], 1):
        flag = "  [explore]" if p.get("explore") else ""
        print(f"\n{i}. {p['id']}  score {p['scores']['total']}{flag}")
        print(f"   {p['summary']}")
        for w in p["why"]:
            print(f"   + {w}")
        freshness = p.get("ref_freshness")
        if isinstance(freshness, dict):
            pinned_at = freshness.get("pinned_commit_at")
            branch = freshness.get("default_branch")
            branch_at = freshness.get("default_branch_commit_at")
            if pinned_at and branch and branch_at:
                if p["pin"]["ref"] == branch:
                    status = "pinned ref is the default branch head"
                elif freshness.get("default_branch_is_newer") is True:
                    minutes = freshness.get("default_branch_ahead_minutes")
                    days = freshness.get("default_branch_ahead_days")
                    if isinstance(minutes, int | float) and minutes < 60:
                        difference = f"{minutes:g} minutes"
                    elif isinstance(minutes, int | float) and minutes < 1440:
                        difference = f"{minutes / 60:.1f} hours"
                    elif isinstance(days, int | float):
                        difference = f"{days:g} days"
                    else:
                        difference = "a newer commit"
                    status = f"{branch} is newer than pinned {p['pin']['ref']} by {difference}"
                else:
                    status = f"{branch} is not newer than pinned {p['pin']['ref']}"
                print(f"   ref freshness: pinned {pinned_at}; branch head {branch_at} ({status})")
            else:
                print("   ref freshness: commit date unavailable")
        for k in p.get("integration_risks", []):
            print(f"   ! {k}")
        for k in p.get("unresolved_checks", []):
            print(f"   ? {k}")
        print(f"   get: {p['get']}")
    if r.get("also_passed"):
        print(f"\nalso passed the gates, ranked lower: {', '.join(r['also_passed'])}")
    if r.get("rejected"):
        print("\nrejected:")
        for x in r["rejected"]:
            kind = "unchecked" if x.get("unchecked") else "failed"
            print(f"   - {x['repo']}: {x['gate']} {kind}: {x['reason']}")
    if r.get("unresolved"):
        print(f"\nunresolved (not real GitHub repos): {', '.join(r['unresolved'])}")
    for n in r.get("notes", []):
        print(f"\nnote: {n}")
    print(
        f"\nconsidered {r['considered']} | relevance: {r['relevance_judge']} | {r['elapsed_ms']} ms | {r['recommendation_id']}"
    )
    if r.get("limits"):
        print(r["limits"])


def cmd_search(args: argparse.Namespace) -> int:
    from reuse_layer.core.engine import Engine

    settings = _load(args)

    async def go() -> dict[str, Any]:
        engine = Engine.create(settings)
        try:
            return await engine.search(args.queries, args.per_query)
        finally:
            await engine.aclose()

    print(json.dumps(asyncio.run(go()), indent=2, ensure_ascii=False))
    return 0


def cmd_install_skill(args: argparse.Namespace) -> int:
    src = resources.files("reuse_layer.integrations").joinpath("skills", SKILL, "SKILL.md")
    base = Path(args.project).expanduser().resolve() / ".claude" if args.project else Path.home() / ".claude"
    dest = base / "skills" / SKILL / "SKILL.md"
    if dest.exists() and not args.force:
        print(f"{dest} exists; pass --force to overwrite", file=sys.stderr)
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    with resources.as_file(src) as s:
        shutil.copyfile(s, dest)
    print(f"Installed the discovery skill at {dest}")
    exe = Path(sys.executable)
    print("\nRegister the MCP server with Claude Code (once):")
    print(f'  claude mcp add reuse-layer --scope user -- "{exe}" -m reuse_layer mcp')
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="reuse-layer", description="Find the right open-source GitHub project to reuse."
    )
    p.add_argument("--version", action="version", version=f"reuse-layer {__version__}")
    p.add_argument("--config", help="path to the config file (default: per-user config directory)")
    p.add_argument(
        "-v", "--verbose", action="count", default=0, help="increase logging (-vv for API diagnostics)"
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("mcp", help="run the MCP server on stdio").set_defaults(fn=cmd_mcp)

    c = sub.add_parser("config", help="create, locate or show the config file")
    c.add_argument("action", choices=["init", "path", "show"])
    c.set_defaults(fn=cmd_config)

    k = sub.add_parser("keys", help="store a key in the OS keychain")
    k.add_argument("op", choices=["set"])
    k.add_argument("name")
    k.set_defaults(fn=cmd_keys)

    sub.add_parser("doctor", help="check the config and the GitHub connection").set_defaults(fn=cmd_doctor)

    f = sub.add_parser("find", help="top 3 GitHub projects for a task")
    f.add_argument("task")
    f.add_argument("--component", help="name of the component this call decides (echoed in the result)")
    f.add_argument(
        "--capability",
        "-c",
        action="append",
        help="a must-have capability (alternatives with '|'); repeatable",
    )
    f.add_argument("--candidate", action="append", help="owner/repo you think fits; repeatable")
    f.add_argument("--query", "-q", action="append", help="a GitHub search query; repeatable")
    f.add_argument("--avoid", action="append", help="a different sense of the words to demote; repeatable")
    f.add_argument("--exclude", action="append", help="owner/repo to leave out; repeatable")
    f.add_argument("--language")
    f.add_argument("--license-policy", choices=["permissive", "weak_copyleft", "any_osi"])
    f.add_argument("--allow-new", action="store_true")
    f.add_argument("--json", action="store_true")
    f.set_defaults(fn=cmd_find)

    s = sub.add_parser("search", help="search GitHub repositories (discovery helper)")
    s.add_argument("queries", nargs="+")
    s.add_argument("--per-query", type=int, default=8)
    s.set_defaults(fn=cmd_search)

    i = sub.add_parser("install-skill", help="install the discovery skill for Claude Code")
    i.add_argument("--project", help="install into this project's .claude/skills instead of ~/.claude/skills")
    i.add_argument("--force", action="store_true")
    i.set_defaults(fn=cmd_install_skill)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.fn(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
