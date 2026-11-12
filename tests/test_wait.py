"""Offline tests for the research wait loop and the truncation logic. No network, no key.

Run: .venv/bin/python test_wait.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

os.environ.setdefault("LINKUP_API_KEY", "test-key-not-real")
from linkup_mcp import server  # noqa: E402

failures = 0


def check(cond: bool, msg: str) -> None:
    global failures
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        failures += 1


def make_client(statuses: list[str]) -> tuple[httpx.AsyncClient, list[int]]:
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(calls[0], len(statuses) - 1)
        calls[0] += 1
        st = statuses[i]
        body = {"id": "t1", "type": "research", "status": st, "input": {"q": "x", "outputType": "sourcedAnswer"},
                "output": {"answer": "A" * 50, "sources": []} if st == "completed" else None,
                "error": "boom" if st == "failed" else None, "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"}
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://mock/v1", headers={"Authorization": "Bearer x"})
    return client, calls


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

    # Truncation: answer survives, snippets shed first
    answer = "Z" * 5000
    obj = {"task": {"output": {"answer": answer, "sources": [{"name": "s", "url": "u", "snippet": "S" * 4000, "favicon": ""} for _ in range(10)]}}}
    out, trunc = server._shrink(json.loads(json.dumps(obj)), 20000)
    text = json.dumps(out, ensure_ascii=False)
    check(trunc and out["task"]["output"]["answer"] == answer, "answer kept whole while snippets are cut")
    check(len(text) <= 20000 + 200, f"fits budget: {len(text)} chars")
    # Only the answer is too long: it is cut, with marker
    obj = {"task": {"output": {"answer": "Q" * 30000, "sources": []}}}
    out, trunc = server._shrink(obj, 10000)
    check(trunc and out["task"]["output"]["answer"].endswith(server.TRUNCATION_MARKER) and len(json.dumps(out)) <= 10300, "oversize answer cut as last resort")
    # Under budget: untouched
    obj = {"a": "x" * 100}
    out, trunc = server._shrink(obj, 1000)
    check(not trunc and out["a"] == "x" * 100, "small object untouched")

    # list summary
    data = {"data": [{"id": "1", "status": "completed", "output": {"answer": "hello world", "sources": [{}]}}, {"id": "2", "status": "pending", "output": None}], "metadata": {}}
    s = server._summarize_tasks(json.loads(json.dumps(data)), False)
    check("output" not in s["data"][0] and s["data"][0]["output_preview"]["answer_chars"] == 11 and s["data"][1]["output_preview"] is None, "list summary replaces outputs")
    s2 = server._summarize_tasks(json.loads(json.dumps(data)), True)
    check("output" in s2["data"][0], "include_output keeps outputs")


asyncio.run(run())
print("RESULT:", "OK" if failures == 0 else f"{failures} failure(s)")
sys.exit(1 if failures else 0)
