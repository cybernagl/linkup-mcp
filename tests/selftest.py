"""Self-test for the Linkup MCP server.

Spawns the server (python -m linkup_mcp) over stdio with the MCP client from the mcp SDK, checks the protocol
(initialize, tools/list, resources/list, tools/call of the free guide tool) and, when
LINKUP_API_KEY is set in the environment, runs free live calls (balance, list research,
list tasks). Paid calls run only with flags:

  python selftest.py                 protocol + free live calls (if key present)
  python selftest.py --paid          adds two fast searches and two fetches (about 0.014 USD)

The key is never printed.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
EXPECTED_TOOLS = {
    "linkup-search", "linkup-fetch",
    "linkup-research", "linkup-get-research", "linkup-wait-research", "linkup-list-research",
    "linkup-tasks-create", "linkup-tasks-get", "linkup-tasks-list",
    "linkup-credits-balance",
    "linkup-extract", "linkup-get-extract", "linkup-list-extract", "linkup-extract-download",
    "linkup-rewind-search", "linkup-rewind-fetch",
    "linkup-responses", "linkup-guide",
}


def text_of(result) -> str:
    parts = []
    for c in result.content:
        if getattr(c, "type", "") == "text":
            parts.append(c.text)
    return "\n".join(parts)


async def main(argv: list[str]) -> int:
    paid = "--paid" in argv
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LINKUP_API_KEY", "LINKUP_BASE_URL", "LINKUP_TIMEOUT")}
    have_key = bool(env.get("LINKUP_API_KEY"))
    params = StdioServerParameters(command=sys.executable, args=["-m", "linkup_mcp"], env=env)
    failures = 0
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"initialize: server {init.serverInfo.name} {init.serverInfo.version}, protocol {init.protocolVersion}")
            print(f"instructions: {len(init.instructions or '')} chars")
            expected_version = importlib.metadata.version("linkup-mcp")
            if init.serverInfo.version != expected_version:
                print(f"FAIL serverInfo.version is {init.serverInfo.version!r}, package version is {expected_version!r}")
                failures += 1

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            missing = EXPECTED_TOOLS - names
            extra = names - EXPECTED_TOOLS
            print(f"tools/list: {len(names)} tools; missing={sorted(missing)} extra={sorted(extra)}")
            for t in tools.tools:
                desc_ok = bool(t.description and len(t.description) > 40)
                props = (t.inputSchema or {}).get("properties", {})
                if not desc_ok:
                    failures += 1
                    print(f"  !! {t.name}: short or missing description")
                undocumented = [p for p, s in props.items() if not s.get("description") and p not in ("page", "sort_by", "sort_direction", "max_chars", "id", "query", "page_size")]
                if undocumented:
                    print(f"  note {t.name}: parameters without description: {undocumented}")
            if missing:
                failures += 1

            res = await session.list_resources()
            uris = sorted(str(r.uri) for r in res.resources)
            print(f"resources/list: {uris}")
            if "linkup://guide" not in uris or "linkup://openapi.json" not in uris:
                failures += 1

            r = await session.call_tool("linkup-guide", {"topic": "research"})
            txt = text_of(r)
            print(f"linkup-guide(research): {'error' if r.isError else 'ok'}, {len(txt)} chars, starts: {txt[:60]!r}")
            if r.isError or "reasoningDepth" not in txt:
                failures += 1

            # Error path without network: bad schema is rejected locally
            r = await session.call_tool("linkup-search", {"query": "x", "output_type": "structured", "structured_output_schema": {"type": "array"}})
            print(f"local validation (bad schema): isError={r.isError}: {text_of(r)[:90]}")
            if not r.isError:
                failures += 1

            # Unknown arguments are rejected locally and named in the error
            r = await session.call_tool("linkup-guide", {"topic": "links", "bogusArg": 1})
            print(f"unknown argument: isError={r.isError}: {text_of(r)[:90]}")
            if not r.isError or "bogusArg" not in text_of(r):
                failures += 1

            # Every tool schema forbids additional properties
            open_schemas = [t.name for t in tools.tools if (t.inputSchema or {}).get("additionalProperties") is not False]
            print(f"additionalProperties false on every tool: {'yes' if not open_schemas else 'NO, open: ' + str(open_schemas)}")
            if open_schemas:
                failures += 1

            if not have_key:
                print("LINKUP_API_KEY not set: skipping live calls")
                return failures

            r = await session.call_tool("linkup-credits-balance", {})
            print(f"linkup-credits-balance: {'ERROR ' + text_of(r)[:200] if r.isError else text_of(r)[:120]}")
            if r.isError:
                failures += 1

            r = await session.call_tool("linkup-list-research", {"page_size": 2})
            body = text_of(r)
            try:
                meta = json.loads(body).get("result", {}).get("metadata")
            except ValueError:
                meta = None
            print(f"linkup-list-research: {'ERROR ' + body[:200] if r.isError else f'ok, metadata={meta}'}")
            if r.isError:
                failures += 1

            r = await session.call_tool("linkup-tasks-list", {"page_size": 2})
            body = text_of(r)
            try:
                j = json.loads(body).get("result", {})
                summary = {"metadata": j.get("metadata"), "quota": j.get("quota")}
            except ValueError:
                summary = body[:200]
            print(f"linkup-tasks-list: {'ERROR ' + body[:200] if r.isError else f'ok, {summary}'}")
            if r.isError:
                failures += 1

            # Free checks on existing tasks: get, wait (returns at once for a finished task), tasks-get, dry-run batch
            r = await session.call_tool("linkup-list-research", {"page_size": 1, "sort_direction": "desc"})
            try:
                first = json.loads(text_of(r))["result"]["data"][0]
            except (ValueError, KeyError, IndexError):
                first = None
            if first:
                rid = first["id"]
                r = await session.call_tool("linkup-get-research", {"id": rid, "max_chars": 3000})
                print(f"linkup-get-research({rid[:8]}...): {'ERROR ' + text_of(r)[:200] if r.isError else 'ok, status=' + str(json.loads(text_of(r))['task'].get('status'))}")
                if r.isError:
                    failures += 1
                r = await session.call_tool("linkup-wait-research", {"id": rid, "max_wait_seconds": 12, "max_chars": 3000})
                body = text_of(r)
                try:
                    j = json.loads(body)
                    print(f"linkup-wait-research: ok, done={j.get('done')} status={j['task'].get('status')} polls={j.get('polls')} waited={j.get('waited_seconds')} s")
                except (ValueError, KeyError):
                    print(f"linkup-wait-research: ERROR {body[:200]}")
                    failures += 1
                r = await session.call_tool("linkup-tasks-get", {"id": rid, "max_chars": 2000})
                print(f"linkup-tasks-get(same id): {'ERROR ' + text_of(r)[:200] if r.isError else 'ok, type=' + str(json.loads(text_of(r))['task'].get('type'))}")
                if r.isError:
                    failures += 1
            else:
                print("no existing research task to test get/wait against")

            r = await session.call_tool("linkup-tasks-create", {"dry_run": True, "tasks": [
                {"type": "search", "input": {"q": "test", "depth": "deep", "outputType": "sourcedAnswer"}},
                {"type": "fetch", "input": {"url": "https://example.com", "renderJs": True, "schema": {"type": "object"}}},
                {"type": "research", "input": {"q": "test", "outputType": "sourcedAnswer", "reasoningDepth": "S"}},
            ]})
            print(f"linkup-tasks-create(dry_run): {'ERROR ' + text_of(r)[:200] if r.isError else text_of(r).replace(chr(10), ' ')[:200]}")
            if r.isError:
                failures += 1

            r = await session.call_tool("linkup-list-extract", {"page_size": 1})
            body = text_of(r)
            print(f"linkup-list-extract: {'error (expected while the beta is not enabled): ' + body[:160] if r.isError else 'list call succeeded (creation may still be gated), ' + body[:100]}")

            # Research answer integrity: default max_chars must not touch the answer of real tasks
            r = await session.call_tool("linkup-list-research", {"page_size": 3, "sort_direction": "desc"})
            try:
                ids = [t["id"] for t in json.loads(text_of(r))["result"]["data"] if t.get("status") == "completed"]
            except (ValueError, KeyError):
                ids = []
            import httpx  # local import: only needed here
            async with httpx.AsyncClient(base_url=env.get("LINKUP_BASE_URL", "https://api.linkup.so/v1"), headers={"Authorization": "Bearer " + env["LINKUP_API_KEY"]}, timeout=60) as raw:
                for rid in ids:
                    await asyncio.sleep(1.1)  # at most one request per second to /research/{id}
                    raw_response = await raw.get(f"/research/{rid}")
                    if raw_response.status_code != 200:
                        print(f"integrity {rid[:8]}...: not checked (raw GET returned HTTP {raw_response.status_code})")
                        continue
                    raw_task = raw_response.json()
                    await asyncio.sleep(1.1)
                    r = await session.call_tool("linkup-get-research", {"id": rid})
                    body = text_of(r)
                    if r.isError:
                        print(f"integrity {rid[:8]}...: ERROR {body[:200]}")
                        failures += 1
                        continue
                    j = json.loads(body)
                    out = (j.get("task") or {}).get("output") or {}
                    raw_out = raw_task.get("output") or {}
                    same = out.get("answer") == raw_out.get("answer")
                    print(f"integrity {rid[:8]}...: answer {len(str(raw_out.get('answer', '')))} chars raw, tool {'identical' if same else 'DIFFERS'}, sources {len(raw_out.get('sources') or [])}, response {len(body)} chars, truncated={j.get('_truncated', False)}, structuredContent={'yes' if r.structuredContent else 'no'}")
                    if not same:
                        failures += 1

            if paid:
                calls = [
                    ("linkup-search fast/searchResults (0.005)", "linkup-search", {"query": "Linkup API pricing", "depth": "fast", "output_type": "searchResults", "max_results": 3, "max_chars": 4000}),
                    ("linkup-search fast/structured (0.006)", "linkup-search", {"query": "Linkup web search API", "depth": "fast", "output_type": "structured", "include_sources": True, "max_chars": 4000,
                                                                              "structured_output_schema": {"type": "object", "properties": {"company": {"type": "string"}, "product": {"type": "string"}}, "required": ["company"]}}),
                    ("linkup-fetch standard (0.001)", "linkup-fetch", {"url": "https://example.com", "max_chars": 3000}),
                    ("linkup-fetch with schema (0.002)", "linkup-fetch", {"url": "https://example.com", "max_chars": 3000,
                                                                         "schema": {"type": "object", "properties": {"title": {"type": "string", "description": "page heading"}, "purpose": {"type": "string"}}},
                                                                         "instructions": "Use the page text only."}),
                ]
                for label, name, args in calls:
                    r = await session.call_tool(name, args)
                    body = text_of(r)
                    print(f"{label}: {'ERROR ' + body[:300] if r.isError else 'ok, ' + body.replace(chr(10), ' ')[:260]}")
                    if r.isError:
                        failures += 1
    return failures


if __name__ == "__main__":
    rc = asyncio.run(main(sys.argv[1:]))
    print("RESULT:", "OK" if rc == 0 else f"{rc} failure(s)")
    sys.exit(1 if rc else 0)
