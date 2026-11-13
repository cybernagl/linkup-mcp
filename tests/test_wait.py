"""Offline tests for the research wait loop and the truncation logic. No network, no key.

Run: .venv/bin/python test_wait.py
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

os.environ.setdefault("LINKUP_API_KEY", "test-key-not-real")
from linkup_mcp import server  # noqa: E402

failures = 0


def check(cond: bool, msg: str) -> None:
    global failures
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        failures += 1


def make_client(statuses: list[str], answer: str = "A" * 50) -> tuple[httpx.AsyncClient, list[int]]:
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(calls[0], len(statuses) - 1)
        calls[0] += 1
        st = statuses[i]
        body = {"id": "t1", "type": "research", "status": st, "input": {"q": "x", "outputType": "sourcedAnswer"},
                "output": {"answer": answer, "sources": []} if st == "completed" else None,
                "error": "boom" if st == "failed" else None, "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"}
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://mock/v1", headers={"Authorization": "Bearer x"})
    return client, calls


def make_seq_client(entries: list[Any]) -> tuple[httpx.AsyncClient, list[int]]:
    """Plays one entry per request, then repeats the last. An entry is a task status (str), an HTTP error status (int) or an exception to raise."""
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        entry = entries[min(calls[0], len(entries) - 1)]
        calls[0] += 1
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, int):
            return httpx.Response(entry, json={"statusCode": entry, "error": {"code": "E", "message": f"failure {entry}", "details": []}})
        body = {"id": "t1", "type": "research", "status": entry, "input": {"q": "x", "outputType": "sourcedAnswer"},
                "output": {"answer": "A" * 50, "sources": []} if entry == "completed" else None, "error": None}
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://mock/v1"), calls


def first_text(result: Any) -> str:
    """Text of the first content block from mcp.call_tool (a list of blocks, or a tuple that starts with it)."""
    blocks = result[0] if isinstance(result, tuple) else result
    return blocks[0].text


async def run() -> None:
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        clock[0] += s

    clock = [0.0]
    server.asyncio.sleep = fake_sleep  # type: ignore[attr-defined]
    server.time.monotonic = lambda: clock[0]  # type: ignore[attr-defined]

    # pending -> processing -> completed
    server._client, calls = make_client(["pending", "processing", "completed"])
    r = await server.linkup_wait_research.__wrapped__("t1", 240, 80000, None) if hasattr(server.linkup_wait_research, "__wrapped__") else await server.linkup_wait_research("t1", 240, 80000, None)
    check(r["done"] is True and r["task"]["status"] == "completed" and r["polls"] == 3, f"completes after 3 polls: {r['polls']} polls, sleeps={sleeps}")
    check(sleeps == [2.0, 4.0], f"backoff 2 s then 4 s: {sleeps}")

    # failed
    sleeps.clear(); clock[0] = 0.0
    server._client, calls = make_client(["processing", "failed"])
    r = await server.linkup_wait_research("t1", 240, 80000, None)
    check(r["done"] is True and r["task"]["status"] == "failed" and r["task"]["error"] == "boom", "returns failed task with error")

    # timeout: always processing, max_wait 20 -> returns done False without exceeding the budget
    sleeps.clear(); clock[0] = 0.0
    server._client, calls = make_client(["processing"])
    r = await server.linkup_wait_research("t1", 20, 80000, None)
    check(r["done"] is False and r["task"]["status"] == "processing", "timeout returns done=false with current state")
    check(clock[0] <= 20, f"did not wait longer than max_wait: {clock[0]} s, sleeps={sleeps}")
    check(max(sleeps) <= 10.0, f"backoff capped at 10 s: {sleeps}")

    # HTTP error mapping
    def err_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"statusCode": 429, "error": {"code": "RATE", "message": "slow down", "details": []}})

    server._client = httpx.AsyncClient(transport=httpx.MockTransport(err_handler), base_url="https://mock/v1")
    try:
        await server.linkup_get_research("t1", 5000)
        check(False, "429 raises ToolError")
    except server.ToolError as exc:
        check("429" in str(exc) and "slow down" in str(exc) and "balance" in str(exc), f"429 mapped with hint: {str(exc)[:120]}")
        check(isinstance(exc, server.LinkupError) and exc.status == 429 and exc.transient is True, "429 raises LinkupError with status 429, transient")

    # One serialisation: compact separators, non-ASCII kept
    check(hasattr(server, "_dumps") and server._dumps({"a": [1, "ü"], "b": None}) == '{"a":[1,"ü"],"b":null}', "_dumps is compact JSON with non-ASCII kept")

    # Truncation: answer survives, snippets shed first
    answer = "Z" * 5000
    obj = {"task": {"output": {"answer": answer, "sources": [{"name": "s", "url": "u", "snippet": "S" * 4000, "favicon": ""} for _ in range(10)]}}}
    out, trunc = server._shrink(json.loads(json.dumps(obj)), 20000)
    text = server._dumps(out)
    check(trunc and out["task"]["output"]["answer"] == answer, "answer kept whole while snippets are cut")
    check(len(text) <= 20000, f"fits budget: {len(text)} chars")
    # Only the answer is too long: it is cut, with marker
    obj = {"task": {"output": {"answer": "Q" * 30000, "sources": []}}}
    out, trunc = server._shrink(obj, 10000)
    check(trunc and out["task"]["output"]["answer"].endswith(server.TRUNCATION_MARKER) and len(server._dumps(out)) <= 10000, "oversize answer cut as last resort")
    # Under budget: untouched
    obj = {"a": "x" * 100}
    out, trunc = server._shrink(obj, 1000)
    check(not trunc and out["a"] == "x" * 100, "small object untouched")

    # Size cap: _finish holds max_chars for the compact JSON, _truncated and _hint included
    def size(o: Any) -> int:
        return len(server._dumps(o))

    # Fetch with a schema: the data payload outranks the image list
    fetch = {"result": {"markdown": "m" * 3000, "data": {"description": "d" * 5000, "title": "t" * 50},
                        "images": [{"alt": "a", "url": f"https://x/{i}.png"} for i in range(400)]}, "cost_usd_estimate": 0.002}
    out = server._finish(copy.deepcopy(fetch), 20000)
    res = out.get("result", {})
    images = res.get("images", [])
    check(res.get("data", {}).get("description") == "d" * 5000 and res.get("markdown") == "m" * 3000, "fetch: data.description and markdown kept whole")
    check(len(images) < 400 and res.get("images_omitted", 0) + len(images) == 400, f"fetch: images pruned and counted: {len(images)} kept, {res.get('images_omitted')} omitted")
    check(size(out) <= 20000, f"fetch: fits 20000: {size(out)} chars")

    # Structured search with sources: the strings of the data dict are primary payload
    structured = {"result": {"data": {"summary": "s" * 6000, "content": "c" * 300},
                             "sources": [{"name": f"n{i}", "url": f"https://example.com/{i}", "snippet": "p" * 900} for i in range(20)]}}
    out = server._finish(copy.deepcopy(structured), 8000)
    data = out.get("result", {}).get("data", {})
    check(data.get("summary") == "s" * 6000 and data.get("content") == "c" * 300, "structured search: both data strings kept whole")
    check(size(out) <= 8000, f"structured search: fits 8000: {size(out)} chars")

    # A list in a data dict is payload, whatever its key: the outer sources go first
    nested = {"result": {"data": {"summary": "s" * 3000, "results": [{"title": f"r{i}", "text": "t" * 170} for i in range(30)]},
                         "sources": [{"name": f"n{i}", "url": f"https://example.com/{i}", "snippet": "p" * 900} for i in range(20)]}}
    out = server._finish(copy.deepcopy(nested), 12000)
    res = out.get("result", {})
    data = res.get("data", {})
    check(data.get("results") == nested["result"]["data"]["results"] and "results_omitted" not in data, f"data.results kept whole: {len(data.get('results', []))} items, results_omitted={data.get('results_omitted')}")
    check(len(res.get("sources", [])) < 20 and size(out) <= 12000, f"outer sources pruned instead: {len(res.get('sources', []))} kept, {size(out)} chars")

    # URLs are never cut, even when they are long and other strings are cut
    sourced = {"result": {"answer": "a" * 2000, "sources": [{"name": f"n{i}", "url": f"https://example.com/{'u' * 277}{i:03d}", "snippet": "p" * 1000} for i in range(10)]},
               "cost_usd_estimate": 0.006, "depth": "standard", "outputType": "sourcedAnswer"}
    urls = [s["url"] for s in sourced["result"]["sources"]]
    out = server._finish(copy.deepcopy(sourced), 6000)
    kept = out.get("result", {}).get("sources", [])
    check(len(urls[0]) == 300 and len(kept) > 0 and all(s.get("url") == urls[i] for i, s in enumerate(kept)), f"URLs of the {len(kept)} remaining sources are unchanged")
    check(size(out) <= 6000, f"URL case: fits 6000: {size(out)} chars")

    # Rows: a list outside the supporting lists loses trailing items, and the cap holds
    rows = [{"name": "n" * 40, "price": "12.50", "url": f"https://example.com/p/{i}", "desc": "x" * 180} for i in range(200)]
    download = {"id": "e1", "rows_in_file": 200, "rows_returned": 200, "creditsUsed": 2.5, "rows": rows}
    out = server._finish(copy.deepcopy(download), 48000)
    kept = out.get("rows", [])
    omitted = out.get("rows_omitted", 0)
    check(size(out) <= 48000 and out.get("_truncated") is True, f"rows: fits 48000 and is marked: {size(out)} chars")
    check(omitted > 0 and omitted + len(kept) == 200 and kept == rows[:len(kept)], f"rows: first {len(kept)} rows kept unchanged, rows_omitted={omitted}")

    # Research answer that fits once the sources are gone: it is not cut
    long_answer = ("Grüße aus Zürich, réponse détaillée. " * 1300)[:46500]
    research = {"done": True, "waited_seconds": 412, "polls": 48, "task": {
        "id": "t1", "type": "research", "status": "completed",
        "input": {"q": "q" * 400, "outputType": "sourcedAnswer", "mode": "research", "reasoningDepth": "L"},
        "output": {"answer": long_answer, "sources": [{"name": f"Source {i}", "url": f"https://example.com/{i}", "snippet": "p" * 900, "favicon": "https://example.com/favicon.ico"} for i in range(20)]},
        "error": None, "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:10:00Z"}}
    out = server._finish(copy.deepcopy(research), 48000)
    output = out.get("task", {}).get("output", {})
    check(output.get("answer") == long_answer, "research: answer of 46500 chars kept whole at max_chars 48000")
    check(size(out) <= 48000 and output.get("sources_omitted", 0) + len(output.get("sources", [])) == 20, f"research: fits 48000 with sources counted: {size(out)} chars, sources_omitted={output.get('sources_omitted')}")

    # Escapes count: an answer full of quotes, backslashes and newlines is cut only as far as needed
    escaped = {"task": {"output": {"answer": 'Line "one"\\ with \n' * 5000, "sources": []}}}
    out = server._finish(copy.deepcopy(escaped), 10000)
    cut = out.get("task", {}).get("output", {}).get("answer", "")
    check(9900 <= size(out) <= 10000 and cut.endswith(server.TRUNCATION_MARKER), f"escaped answer cut to the cap, not below: {size(out)} chars")

    # Terminal fallback: nothing to cut, so the start of the JSON text is returned
    many = {f"key{i}": i for i in range(3000)}
    text = server._dumps(many)
    out = server._finish(copy.deepcopy(many), 2000)
    prefix = out.get("_raw_prefix")
    longest = isinstance(prefix, str) and size({**out, "_raw_prefix": text[:len(prefix) + 1]}) > 2000
    check(size(out) <= 2000 and out.get("_truncated") is True and isinstance(prefix, str) and text.startswith(prefix) and longest,
          f"fallback: fits 2000 with the longest _raw_prefix: {size(out)} chars, prefix {len(prefix) if isinstance(prefix, str) else None} chars")

    # Speed: pruning does not serialise the whole object once per dropped item
    many_rows = {"rows": [{"name": "n" * 40, "price": "12.50", "url": f"https://example.com/p/{i}", "desc": "x" * 180} for i in range(5000)]}
    started = time.perf_counter()
    out = server._finish(many_rows, 48000)
    elapsed = time.perf_counter() - started
    check(elapsed < 10, f"5000 rows cut in {elapsed:.2f} s")
    check(size(out) <= 48000, f"5000 rows: fits 48000: {size(out)} chars")

    # The text that the MCP server sends is the compact JSON that was measured
    server._client, calls = make_client(["completed"], answer="B" * 30000)
    text = first_text(await server.mcp.call_tool("linkup-get-research", {"id": "t1", "max_chars": 5000}))
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    check(len(text) <= 5000 and parsed is not None and text == server._dumps(parsed), f"sent text is compact JSON within max_chars: {len(text)} chars")

    # The registered wrapper keeps the parameter schema and hides ctx
    tools = {t.name: t for t in await server.mcp.list_tools()}
    prop = tools["linkup-search"].inputSchema["properties"].get("output_type", {})
    check(bool(prop.get("description")) and prop.get("default") == "searchResults", "linkup-search schema keeps output_type with description and default")
    check("ctx" not in tools["linkup-wait-research"].inputSchema["properties"], "linkup-wait-research schema has no ctx")

    # The registered wrapper still injects ctx
    sleeps.clear(); clock[0] = 0.0
    server._client, calls = make_client(["completed"])
    text = first_text(await server.mcp.call_tool("linkup-wait-research", {"id": "t1", "max_wait_seconds": 20}))
    check('"done":true' in text, f"linkup-wait-research through the MCP server: {text[:60]}")

    # list summary
    data = {"data": [{"id": "1", "status": "completed", "output": {"answer": "hello world", "sources": [{}]}}, {"id": "2", "status": "pending", "output": None}], "metadata": {}}
    s = server._summarize_tasks(json.loads(json.dumps(data)), False)
    check("output" not in s["data"][0] and s["data"][0]["output_preview"]["answer_chars"] == 11 and s["data"][1]["output_preview"] is None, "list summary replaces outputs")
    s2 = server._summarize_tasks(json.loads(json.dumps(data)), True)
    check("output" in s2["data"][0], "include_output keeps outputs")

    # Wait survives transient errors
    sleeps.clear(); clock[0] = 0.0
    server._client, calls = make_seq_client(["processing", 504, 429, httpx.ConnectError("down"), "completed"])
    r = await server.linkup_wait_research("t1", 240, 80000, None)
    check(r["done"] is True and r["polls"] == 5 and r["transient_errors"] == 3, f"wait survives 504, 429, ConnectError: polls={r.get('polls')}, transient_errors={r.get('transient_errors')}")

    # A clean wait has no transient_errors key
    sleeps.clear(); clock[0] = 0.0
    server._client, calls = make_seq_client(["completed"])
    r = await server.linkup_wait_research("t1", 240, 80000, None)
    check(r["done"] is True and "transient_errors" not in r, "clean wait has no transient_errors")

    # Wait gives up cleanly when every poll fails
    sleeps.clear(); clock[0] = 0.0
    server._client, calls = make_seq_client([500])
    r = await server.linkup_wait_research("t1", 20, 80000, None)
    check(r["done"] is False and r["task"] is None and "500" in r["last_error"] and r["id"] == "t1", f"every poll 500: done={r.get('done')}, task={r.get('task')}")
    check(r["transient_errors"] == r["polls"] == calls[0] and r["next"].startswith("No poll succeeded"), f"failed polls counted: polls={r.get('polls')}, requests={calls[0]}")
    check(clock[0] <= 20, f"every poll 500: clock {clock[0]} s")

    # done false after a successful poll then an error keeps the last task state
    sleeps.clear(); clock[0] = 0.0
    server._client, calls = make_seq_client(["processing", 503])
    r = await server.linkup_wait_research("t1", 20, 80000, None)
    check(r["done"] is False and r["task"]["status"] == "processing" and r["transient_errors"] >= 1 and r["next"].startswith("Still running"), "error after a good poll keeps the last task")

    # Wait fails fast on a non-transient error
    sleeps.clear(); clock[0] = 0.0
    server._client, calls = make_seq_client([404])
    try:
        await server.linkup_wait_research("t1", 240, 80000, None)
        check(False, "404 raises ToolError")
    except server.ToolError as exc:
        check(calls[0] == 1 and isinstance(exc, server.LinkupError) and exc.status == 404 and exc.transient is False, f"404 fails fast after {calls[0]} request")

    # A non-transient error after transient ones is raised
    sleeps.clear(); clock[0] = 0.0
    server._client, calls = make_seq_client([502, 401])
    try:
        await server.linkup_wait_research("t1", 240, 80000, None)
        check(False, "401 after 502 raises ToolError")
    except server.ToolError:
        check(calls[0] == 2, f"401 after 502 raised after {calls[0]} requests")

    # Network errors have no status
    server._client, calls = make_seq_client([httpx.ConnectError("down")])
    try:
        await server.linkup_get_research("t1", 5000)
        check(False, "ConnectError raises LinkupError")
    except server.LinkupError as exc:
        check(exc.status is None and exc.transient is True, "network error: status None, transient")

    # done false is capped
    sleeps.clear(); clock[0] = 0.0
    server._client, calls = make_client(["processing"])
    big_task = {"id": "t1", "type": "research", "status": "processing", "input": {"q": "q" * 100000, "outputType": "sourcedAnswer"}, "output": None, "error": None}
    server._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=big_task)), base_url="https://mock/v1")
    r = await server.linkup_wait_research("t1", 20, 5000, None)
    check(size(r) <= 5000, f"done false held to 5000: {size(r)} chars")

    # Extract paths hold the cap
    big_extract = {"id": "e1", "status": "completed", "input": {"q": "q" * 50000, "url": "https://example.com"}, "output": {"resultUrl": "https://x/r", "creditsUsed": 2.5}}
    server._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=big_extract)), base_url="https://mock/v1")
    r = await server.linkup_get_extract("e1", 5000)
    check(size(r) <= 5000, f"linkup-get-extract held to 5000: {size(r)} chars")

    page = {"data": [{"id": str(i), "status": "completed", "input": {"q": "q" * 500}, "output": {"rows": ["r" * 100] * 50}} for i in range(100)], "metadata": {"page": 1}}
    server._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=page)), base_url="https://mock/v1")
    r = await server.linkup_list_extract(1, 100, None, "desc", False, 5000)
    first = r.get("result", {}).get("data", [{}])[0]
    check(size(r) <= 5000, f"linkup-list-extract held to 5000: {size(r)} chars")
    check("output_preview" in first and "output" not in first, "linkup-list-extract gives output_preview in place of output")

    submitted = [{"id": f"t{i}", "type": "search", "status": "pending", "input": {"q": "q" * 400}, "output": None} for i in range(100)]
    server._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=submitted)), base_url="https://mock/v1")
    r = await server.linkup_tasks_create([{"type": "search", "input": {"q": "x", "depth": "fast", "outputType": "searchResults"}}], False, 5000)
    check(size(r) <= 5000, f"linkup-tasks-create held to 5000: {size(r)} chars")
    r = await server.linkup_tasks_create([{"type": "search", "input": {"q": "x", "depth": "fast", "outputType": "searchResults"}}], True, 5000)
    check(r.get("dry_run") is True, "linkup-tasks-create dry run unchanged")

    # Redirects: the download client follows them
    check(server._download_client().follow_redirects is True, "download client follows redirects")
    original_download_client = server._download_client

    def ndjson_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/first":
            return httpx.Response(302, headers={"Location": "https://files.example/second"})
        return httpx.Response(200, text='{"a":1}\n{"a":2}\n')

    server._download_client = lambda **kw: original_download_client(transport=httpx.MockTransport(ndjson_handler), **kw)
    task = {"id": "e1", "status": "completed", "output": {"resultUrl": "https://files.example/first", "creditsUsed": 2.5}}
    server._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=task)), base_url="https://mock/v1")
    r = await server.linkup_extract_download("e1", 200, 48000)
    check(r.get("rows") == [{"a": 1}, {"a": 2}], f"download follows a 302: {r.get('rows')}")

    # Row count matches the rows in the response after the cap
    lines = "\n".join(json.dumps({"n": i, "text": "x" * 100}) for i in range(300))
    server._download_client = lambda **kw: original_download_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=lines)), **kw)
    r = await server.linkup_extract_download("e1", 200, 20000)
    kept = r.get("rows", [])
    check(r["rows_in_file"] == 300 and r["rows_returned"] == len(kept) and r["rows_omitted"] == 200 - len(kept) and size(r) <= 20000,
          f"row count after cap: returned={r.get('rows_returned')}, kept={len(kept)}, omitted={r.get('rows_omitted')}, {size(r)} chars")
    server._download_client = original_download_client


asyncio.run(run())
print("RESULT:", "OK" if failures == 0 else f"{failures} failure(s)")
sys.exit(1 if failures else 0)
