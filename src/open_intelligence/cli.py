"""Command line: run the MCP server, or try routing and sources from a shell.

open-intelligence serve                     # MCP server over stdio (what an MCP client runs)
open-intelligence route "<task>"            # offline: which sources, and why
open-intelligence sources [--check]         # config and auth state; --check connects
open-intelligence search "<task>" -q "..."  # route + live search, raw results
open-intelligence skill-path                # where the routing skill (SKILL.md) is installed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from importlib import resources
from typing import Any

from open_intelligence import __version__
from open_intelligence.config import ConfigError, load_settings
from open_intelligence.connector import Connector
from open_intelligence.router import route
from open_intelligence.sources import NEEDS


def _print(data: Any) -> None:
    # Always UTF-8, also when Windows redirects stdout with a legacy code page.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(data, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open-intelligence", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", help="path to a TOML config file (see open-intelligence.example.toml)")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="run the MCP server over stdio (default)")

    p_route = sub.add_parser("route", help="show which sources a task routes to (offline)")
    p_route.add_argument("task")
    p_route.add_argument(
        "--need", action="append", choices=NEEDS, help="decide the needs yourself (repeatable)"
    )

    p_sources = sub.add_parser("sources", help="show configured sources")
    p_sources.add_argument(
        "--check", action="store_true", help="connect to each source and list allowed tools"
    )

    p_search = sub.add_parser("search", help="route a task and search the routed sources (live)")
    p_search.add_argument("task")
    p_search.add_argument("-q", "--query", required=True, help="short search keywords")
    p_search.add_argument("--need", action="append", choices=NEEDS)
    p_search.add_argument("--limit", type=int, default=5)

    sub.add_parser("skill-path", help="print the path of the bundled routing skill")

    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "route":
        _print(route(args.task, args.need).as_dict())
        return 0
    if command == "skill-path":
        print(resources.files("open_intelligence").joinpath("skills/open-source-routing"))
        return 0

    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    connector = Connector(settings)

    if command == "serve":
        from open_intelligence.server import build_server

        server = build_server(connector)
        # One log line per upstream HTTP request is noise in an MCP client's log; keep warnings.
        for name in ("httpx", "httpx2", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)
        server.run("stdio")
        return 0
    if command == "sources":
        return asyncio.run(_sources(connector, args.check))
    if command == "search":
        return asyncio.run(_search(connector, args.task, args.query, args.need, args.limit))
    parser.error(f"unknown command {command}")
    return 2


async def _sources(connector: Connector, check: bool) -> int:
    results = [(await connector.check(s, connect=check)).as_dict() for s in connector.settings.sources]
    _print(results)
    return 0


async def _search(connector: Connector, task: str, query: str, needs: list[str] | None, limit: int) -> int:
    from open_intelligence.server import _run_searches

    decision = route(task, needs)
    results = await _run_searches(connector, decision, query, {}, max(1, min(limit, 10)))
    _print({"routing": decision.as_dict(), "results": results})
    return 0
