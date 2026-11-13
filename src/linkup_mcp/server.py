"""Linkup MCP server: full coverage of the Linkup REST API (api.linkup.so/v1).

One tool per API operation, plus a time-boxed poller for research tasks, a
downloader for extract results, and a built-in guide. Tool descriptions carry
the operational facts from Linkup's documentation (read 2026-11-13): when to
use which endpoint, latency, price, limits, polling rules.

Configuration (environment):
  LINKUP_API_KEY   required, sent as "Authorization: Bearer <key>"
  LINKUP_BASE_URL  optional, default https://api.linkup.so/v1
  LINKUP_TIMEOUT   optional, HTTP timeout in seconds for one request, default 180

Run: linkup-mcp   (stdio transport; or python -m linkup_mcp)
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _package_version

    __version__ = _package_version("linkup-mcp")
except PackageNotFoundError:  # running from a plain source tree, not installed
    __version__ = "0+unknown"

DOCS_READ_DATE = "2026-11-13"
HERE = Path(__file__).resolve().parent
OPENAPI_SNAPSHOT = HERE / "openapi.json"

# --------------------------------------------------------------------------
# Prices in USD per call, from https://docs.linkup.so/pages/documentation/platform/pricing
# (read 2026-11-13). Used for estimates only; Linkup bills the real amount.
# --------------------------------------------------------------------------
SEARCH_PRICE = {
    ("flash", "searchResults"): 0.005,
    ("fast", "searchResults"): 0.005,
    ("standard", "searchResults"): 0.005,
    ("deep", "searchResults"): 0.05,
}
RESEARCH_PRICE = {"S": 0.25, "M": 0.50, "L": 1.50, "XL": 2.50}


def search_price(depth: str, output_type: str) -> float:
    base = SEARCH_PRICE.get((depth, "searchResults"), 0.005)
    if output_type in ("sourcedAnswer", "structured"):
        return base + (0.005 if depth == "deep" else 0.001)
    return base


def fetch_price(mode: str, render_js: bool, has_schema: bool) -> float:
    if mode == "pro":
        base = 0.01 if render_js else 0.005
    else:
        base = 0.005 if render_js else 0.001
    return base + (0.001 if has_schema else 0.0)


# --------------------------------------------------------------------------
# Server-level instructions (injected into the client's context)
# --------------------------------------------------------------------------
INSTRUCTIONS = f"""Linkup web access (docs read {DOCS_READ_DATE}; prices in USD per call, billed from a prepaid balance).

Routing:
- Known URL, want the page: linkup-fetch (0.001 to 0.011). Add renderJs for JavaScript sites, mode "pro" for hard pages, schema for typed JSON from that one page.
- Question answerable by a web search: linkup-search. depth "fast" (about 1 s, keyword-only, 0.005) is the default; "standard" (1 to 3 s, one agentic pass, follows instructions, can scrape one URL in the query); "deep" (5 to 30 s, several iterations, sequential instructions, 0.05 to 0.055). outputType "searchResults" when you will reason over the sources yourself, "sourcedAnswer" for a cited answer, "structured" with a JSON schema for typed output.
- Multi-source synthesis that one search cannot resolve: linkup-research, asynchronous, 2 to 20 minutes, 0.25 (S) to 2.50 (XL) per task, default L = 1.50. Set mode explicitly (answer, investigate, research). Then linkup-wait-research or linkup-get-research. Never re-create a task to retry a wait; every creation is billed.
- Bulk or mixed work (up to 100 items per batch, same price per item): linkup-tasks-create, then linkup-tasks-list or linkup-tasks-get.
- Balance: linkup-credits-balance. A 429 means rate limit or no credit left.
- Reference: linkup-guide (topics) and the resources linkup://guide and linkup://openapi.json.

Rules from Linkup's docs: put date ranges in fromDate/toDate, not in the query; prefer a few trusted includeDomains; keep structured schemas flat; poll research at 2 s backing off to 10 s, never above 1 request per second; failed calls are not billed. Extract and Rewind are closed betas (403 unless enabled for the organisation)."""

mcp = FastMCP(
    "linkup",
    instructions=INSTRUCTIONS,
    website_url="https://github.com/cybernagl/linkup-mcp",
    log_level="WARNING",
)
# FastMCP (mcp 1.x) has no version parameter and reports the SDK's version in the
# initialize response; the low-level server underneath does take one.
mcp._mcp_server.version = __version__
logging.getLogger("httpx").setLevel(logging.WARNING)

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
# Synchronous paid calls change nothing on the server side; they are read-only in MCP terms.
PAID = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)
# Calls that create a task (billed, listed later).
CREATES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


def _dumps(obj: Any) -> str:
    """Serialise to compact JSON with non-ASCII characters kept. The tools send this text, and the size cap measures it."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def tool(**options: Any):
    """Register a tool with FastMCP and return the function unchanged.

    FastMCP gets a wrapper that sends a dict result as the text of _dumps, the text that the
    size cap measures. A str result goes out unchanged. Structured output is off: one text
    block per result, not text plus a JSON copy. functools.wraps keeps the parameter schema
    and the injection of ctx.
    """

    def register(fn: Any) -> Any:
        @functools.wraps(fn)
        async def send(*args: Any, **kwargs: Any) -> Any:
            result = await fn(*args, **kwargs)
            return result if isinstance(result, str) else _dumps(result)

        mcp.tool(structured_output=False, **options)(send)
        return fn

    return register


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        key = os.environ.get("LINKUP_API_KEY", "").strip()
        if not key:
            raise ToolError("LINKUP_API_KEY is not set in the server environment.")
        base = os.environ.get("LINKUP_BASE_URL", "https://api.linkup.so/v1").rstrip("/")
        timeout = float(os.environ.get("LINKUP_TIMEOUT", "180"))
        _client = httpx.AsyncClient(
            base_url=base,
            headers={"Authorization": f"Bearer {key}", "User-Agent": "linkup-mcp-local/1.0"},
            timeout=httpx.Timeout(timeout, connect=20.0),
        )
    return _client


STATUS_HINTS = {
    400: "Bad request: a parameter is missing or invalid, the search found nothing, or the fetch target is too large, unreachable or of an unsupported type.",
    401: "Unauthorized: the API key is missing or invalid.",
    402: "Payment required: no API key was sent (x402 flow).",
    403: "Forbidden: this key or organisation has no access to the endpoint (Extract and Rewind are closed betas).",
    404: "Not found.",
    409: "Conflict.",
    422: "Unprocessable: the request cannot be served for the given day.",
    429: "Too many requests: rate limit exceeded or the credit balance is used up (check linkup-credits-balance).",
    500: "Linkup internal error.",
    504: "Gateway timeout: the request deadline was exceeded.",
}


async def _request(method: str, path: str, *, json_body: Any = None, params: dict[str, Any] | None = None) -> Any:
    client = _get_client()
    try:
        resp = await client.request(method, path, json=json_body, params=params)
    except httpx.TimeoutException as exc:
        raise ToolError(f"Timeout talking to Linkup ({exc}). Increase LINKUP_TIMEOUT or use a lower depth.") from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"Network error talking to Linkup: {exc}") from exc
    if resp.status_code >= 400:
        detail: Any
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text[:2000]
        hint = STATUS_HINTS.get(resp.status_code, "")
        raise ToolError(f"Linkup HTTP {resp.status_code}. {hint} Response: {json.dumps(detail)[:3000]}")
    if resp.status_code == 204 or not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
JsonSchema = dict[str, Any] | str | None


def _schema_as_string(schema: JsonSchema, field: str) -> str | None:
    """Search and Research take the JSON schema as a string (as the official SDK sends it)."""
    if schema is None:
        return None
    if isinstance(schema, str):
        try:
            parsed = json.loads(schema)
        except ValueError as exc:
            raise ToolError(f"{field} is not valid JSON: {exc}") from exc
    else:
        parsed = schema
    if not isinstance(parsed, dict) or parsed.get("type") != "object":
        raise ToolError(f'{field} must be a JSON Schema whose root has "type": "object".')
    return json.dumps(parsed)


def _schema_as_object(schema: JsonSchema, field: str) -> dict[str, Any] | None:
    if schema is None:
        return None
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except ValueError as exc:
            raise ToolError(f"{field} is not valid JSON: {exc}") from exc
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ToolError(f'{field} must be a JSON Schema whose root has "type": "object".')
    return schema


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


TRUNCATE_FIRST = {"rawHtml", "rawContent", "snippet", "content", "favicon"}  # supporting material
TRUNCATE_LAST = {"answer", "markdown", "output_text", "data"}  # the primary payload
TRUNCATION_MARKER = " [truncated by the MCP server; raise max_chars for more]"
PRUNE_LISTS = {"sources", "results", "images"}  # supporting lists
NEVER_CUT = {"url", "resultUrl"}  # a cut URL is a broken link
CUT_HINT = "Output was cut to fit max_chars={max_chars}. Order of cuts: supporting strings, then trailing list items (the <name>_omitted counters give the numbers), the main text last. Call again with a larger max_chars to see more."
RAW_HINT = "Output could not be cut to max_chars={max_chars} in a structured way. _raw_prefix holds the start of the JSON text. Call again with a larger max_chars."


def _long_strings(obj: Any, tier: int, floor: int) -> list[tuple[int, Any, Any]]:
    """Find the strings of one tier that are longer than floor, as (length, container, key).

    The tier comes from the key of the string: 0 for TRUNCATE_FIRST, 2 for TRUNCATE_LAST,
    1 for all other keys. A string directly in a list uses the key of the list. Every string
    in a dict under the key "data" is tier 2, at any depth: that dict is the primary payload,
    and its keys are field names from the schema of the caller. Strings under a key in
    NEVER_CUT are not found.
    """
    found: list[tuple[int, Any, Any]] = []

    def walk(node: Any, name: Any, payload: bool) -> None:
        if isinstance(node, dict):
            entries = [(k, v, k, payload or (k == "data" and isinstance(v, dict))) for k, v in node.items()]
        elif isinstance(node, list):
            entries = [(i, v, name, payload) for i, v in enumerate(node)]
        else:
            return
        for key, value, own, in_payload in entries:
            if not isinstance(value, str):
                walk(value, own, in_payload)
            elif len(value) > floor and own not in NEVER_CUT:
                own_tier = 2 if in_payload or own in TRUNCATE_LAST else 0 if own in TRUNCATE_FIRST else 1
                if own_tier == tier:
                    found.append((len(value), node, key))

    walk(obj, None, False)
    return found


def _cut_strings(obj: Any, tier: int, floor: int, over: int) -> bool:
    """Cut the longest strings of one tier to a common length, not below floor, to save over characters.

    Returns True when a string was cut.
    """
    group = _long_strings(obj, tier, floor)
    if not group:
        return False
    texts = [(container[key], len(_dumps(container[key]))) for _, container, key in group]

    def saved(level: int) -> int:
        # A cut string keeps level characters. Measure the JSON text of the kept start,
        # because escapes make it longer than its number of characters.
        keep = level - len(TRUNCATION_MARKER)
        return sum(full - len(_dumps(text[:keep])) - len(TRUNCATION_MARKER) for text, full in texts if len(text) > level)

    if saved(floor) < over:
        level = floor
    else:
        lo, hi = floor, max(length for length, _, _ in group)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if saved(mid) >= over:
                lo = mid
            else:
                hi = mid
        level = lo
    for length, container, key in group:
        if length > level:
            container[key] = container[key][: level - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return True


def _dict_lists(obj: Any) -> list[tuple[dict[str, Any], str]]:
    """Find every list that is the direct value of a dict key, as (dict, key). An outer list comes before the lists in it."""
    found: list[tuple[dict[str, Any], str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, list):
                    found.append((node, k))
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return found


def _drop_trailing(container: dict[str, Any], key: str, keep: int, need: int) -> tuple[int, list[Any]]:
    """Drop trailing items of container[key] until the JSON text is need characters shorter or keep items are left.

    Adds the number of dropped items to container["<key>_omitted"]. The saving is calculated
    from the items, without a serialisation of the whole object: an item saves its JSON text
    and one comma (the last item of the list has no comma to save), and a new counter costs
    ,"<key>_omitted":<n>. Drops nothing when that saves nothing. Returns the number of
    characters saved and the dropped items.
    """
    items = container[key]
    counter = f"{key}_omitted"
    before = container.get(counter, 0)
    if not isinstance(before, int):
        return 0, []
    counter_base = -len(str(before)) if counter in container else len(_dumps(counter)) + 2
    removed = dropped = saved = 0
    while len(items) - dropped > keep and saved < need:
        dropped += 1
        removed += len(_dumps(items[-dropped])) + 1
        last_comma = 1 if dropped == len(items) else 0
        saved = removed - last_comma - (counter_base + len(str(before + dropped)))
    if saved <= 0:
        return 0, []
    tail = items[-dropped:]
    del items[-dropped:]
    container[counter] = before + dropped
    return saved, tail


def _prune_lists(targets: list[tuple[dict[str, Any], str]], keep: int, over: int) -> bool:
    """Drop trailing items of the target lists, one list after the other, to save over characters.

    Skips a list that was in an item dropped before: it is no longer part of the object.
    Returns True when an item was dropped.
    """
    detached: set[int] = set()
    changed = False
    for container, key in targets:
        if over <= 0:
            break
        if id(container) in detached:
            continue
        saved, dropped = _drop_trailing(container, key, keep, over)
        over -= saved
        changed = changed or bool(dropped)
        detached.update(id(inner) for inner, _ in _dict_lists(dropped))
    return changed


def _raw_fallback(text: str, budget: int, max_chars: int) -> dict[str, Any]:
    """Wrap the longest prefix of a JSON text for which the wrapper fits the budget."""
    hint = RAW_HINT.format(max_chars=max_chars)
    lo, hi = 0, min(len(text), budget)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        # Measure the wrapper, not the prefix: JSON escapes the quotes and backslashes in it.
        if len(_dumps({"_truncated": True, "_hint": hint, "_raw_prefix": text[:mid]})) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return {"_truncated": True, "_hint": hint, "_raw_prefix": text[:lo]}


def _shrink(obj: Any, budget: int) -> tuple[Any, bool]:
    """Cut the object until its JSON text (_dumps) fits the budget.

    The steps run in this order and stop when the object fits:
    1. Supporting strings (raw content, source snippets, result content) are cut to a common length, not below 200 characters
    2. All other strings, except the primary payload, are cut the same way
    3. Supporting lists (sources, results, images) lose trailing items, down to 5 items each
    4. Supporting lists lose trailing items, down to 0 items
    5. The primary payload (answer, markdown, output_text, every string in a data dict) is cut to a common length, not below 1000 characters
    6. Every other list under a dict key loses trailing items, the largest list first, down to 1 item each
    7. If the object still does not fit, the result is a new dict with _truncated, _hint and _raw_prefix (the start of the JSON text)

    A cut string ends with TRUNCATION_MARKER. A URL is never cut. A <key>_omitted counter
    next to a list gives the number of dropped items. Steps 1 to 6 change the object in
    place. Step 7 returns a new dict, so callers must use the returned object.
    Returns (object, truncated).
    """
    over = len(_dumps(obj)) - budget
    if over <= 0:
        return obj, False

    def supporting_lists() -> list[tuple[dict[str, Any], str]]:
        return [(c, k) for c, k in _dict_lists(obj) if k in PRUNE_LISTS]

    def other_lists_largest_first() -> list[tuple[dict[str, Any], str]]:
        others = [(c, k) for c, k in _dict_lists(obj) if k not in PRUNE_LISTS]
        return sorted(others, key=lambda t: len(_dumps(t[0][t[1]])), reverse=True)

    steps = (
        lambda need: _cut_strings(obj, 0, 200, need),
        lambda need: _cut_strings(obj, 1, 200, need),
        lambda need: _prune_lists(supporting_lists(), 5, need),  # fewer sources before a shorter answer
        lambda need: _prune_lists(supporting_lists(), 0, need),
        lambda need: _cut_strings(obj, 2, 1000, need),
        lambda need: _prune_lists(other_lists_largest_first(), 1, need),
    )
    for step in steps:
        if step(over):
            over = len(_dumps(obj)) - budget
            if over <= 0:
                return obj, True
    return _raw_fallback(_dumps(obj), budget, budget), True


def _finish(obj: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Fit obj into max_chars (at least 2000) characters of JSON text. The _truncated and _hint keys count toward the cap."""
    cap = max(2000, max_chars)
    if len(_dumps(obj)) <= cap:
        return obj
    marks = {"_truncated": True, "_hint": CUT_HINT.format(max_chars=max_chars)}
    # In the dict, the marks cost their JSON text without the braces, plus one comma.
    out, _ = _shrink(obj, cap - (len(_dumps(marks)) - 1))
    if out is not obj:
        # Terminal fallback. Steps 1 to 6 changed obj in place: wrap its JSON text again,
        # for the full cap and with the max_chars of the caller in the hint.
        return _raw_fallback(_dumps(obj), cap, max_chars)
    out.update(marks)
    return out


def _summarize_tasks(data: Any, include_output: bool) -> Any:
    """Replace each task's output with a short preview unless the caller asked for outputs."""
    if include_output or not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return data
    out = dict(data)
    tasks = []
    for t in data["data"]:
        if not isinstance(t, dict):
            tasks.append(t)
            continue
        t2 = {k: v for k, v in t.items() if k != "output"}
        o = t.get("output")
        if isinstance(o, dict):
            if isinstance(o.get("answer"), str):
                t2["output_preview"] = {"answer_chars": len(o["answer"]), "answer_start": o["answer"][:160], "sources": len(o.get("sources") or [])}
            elif isinstance(o.get("markdown"), str):
                t2["output_preview"] = {"markdown_chars": len(o["markdown"])}
            elif isinstance(o.get("results"), list):
                t2["output_preview"] = {"results": len(o["results"])}
            elif "resultUrl" in o:
                t2["output_preview"] = {"rowsReturned": o.get("rowsReturned"), "creditsUsed": o.get("creditsUsed")}
            else:
                t2["output_preview"] = {"keys": list(o.keys())[:12]}
        else:
            t2["output_preview"] = None
        tasks.append(t2)
    out["data"] = tasks
    out["_note"] = "outputs replaced by output_preview; pass include_output=true or use the get tool for a full output"
    return out


def _page_params(page: int, page_size: int, sort_by: str | None, sort_direction: str | None) -> dict[str, Any]:
    if not 1 <= page_size <= 100:
        raise ToolError("page_size must be between 1 and 100.")
    if page < 1:
        raise ToolError("page must be at least 1.")
    return _drop_none({"page": page, "pageSize": page_size, "sortBy": sort_by, "sortDirection": sort_direction})


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
@tool(
    name="linkup-search",
    title="Linkup web search",
    annotations=PAID,
)
async def linkup_search(
    query: Annotated[str, Field(description="Natural-language query. flash and fast are keyword-only (write 'NVIDIA Q4 2024 revenue'); standard and deep follow instructions (name what to retrieve, then how to shape the answer; standard can scrape one URL given in the query, deep several, in order).")],
    depth: Annotated[Literal["flash", "fast", "standard", "deep"], Field(description="flash: ranked snippets in under 200 ms, no LLM (alpha). fast: one-shot keyword retrieval in about 1 s, the recommended default. standard: one agentic pass, 1 to 3 s, can fan out into parallel sub-searches. deep: several iterations with sequential instructions, 5 to 30 s.")] = "fast",
    output_type: Annotated[Literal["searchResults", "sourcedAnswer", "structured"], Field(description="searchResults: raw results (name, url, content, publishedAt) for you to reason over. sourcedAnswer: an answer plus sources. structured: JSON matching structured_output_schema.")] = "searchResults",
    structured_output_schema: Annotated[JsonSchema, Field(description="Required when output_type is structured. JSON Schema (object or string) with root type object. Keep it flat.")] = None,
    include_sources: Annotated[bool | None, Field(description="structured only: also return the sources; the response then has data and sources.")] = None,
    include_inline_citations: Annotated[bool | None, Field(description="sourcedAnswer only: add inline citations to the answer text.")] = None,
    include_images: Annotated[bool | None, Field(description="Also return image results.")] = None,
    max_results: Annotated[int | None, Field(description="Upper bound on the number of results (minimum 1).", ge=1)] = None,
    include_domains: Annotated[list[str] | None, Field(description="Restrict to these domains (up to 100). Recommended when authoritative sources are known.")] = None,
    exclude_domains: Annotated[list[str] | None, Field(description="Domains to exclude.")] = None,
    from_date: Annotated[str | None, Field(description="YYYY-MM-DD. Put date ranges here, not in the query.")] = None,
    to_date: Annotated[str | None, Field(description="YYYY-MM-DD.")] = None,
    max_chars: Annotated[int, Field(description="Cap on the size of the returned JSON; long strings are truncated and _truncated is set.", ge=2000)] = 40000,
) -> dict[str, Any]:
    """Search the web with Linkup (POST /v1/search, synchronous).

    Cost per call: flash, fast or standard 0.005 USD (searchResults) or 0.006 (sourcedAnswer,
    structured); deep 0.05 or 0.055. Rate limit 10 requests per second per organisation.
    A query without results returns HTTP 400 and is not billed. LinkedIn profile, company
    and post pages can only be read through this endpoint (give the exact URL in the query
    with standard or deep), not through linkup-fetch.
    """
    if output_type == "structured" and structured_output_schema is None:
        raise ToolError("output_type 'structured' needs structured_output_schema.")
    if include_domains and len(include_domains) > 100:
        raise ToolError("include_domains takes at most 100 domains.")
    body = _drop_none(
        {
            "q": query,
            "depth": depth,
            "outputType": output_type,
            "structuredOutputSchema": _schema_as_string(structured_output_schema, "structured_output_schema"),
            "includeSources": include_sources,
            "includeInlineCitations": include_inline_citations,
            "includeImages": include_images,
            "maxResults": max_results,
            "includeDomains": include_domains,
            "excludeDomains": exclude_domains,
            "fromDate": from_date,
            "toDate": to_date,
        }
    )
    data = await _request("POST", "/search", json_body=body)
    out = {"result": data, "cost_usd_estimate": search_price(depth, output_type), "depth": depth, "outputType": output_type}
    return _finish(out, max_chars)


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------
@tool(name="linkup-fetch", title="Linkup page fetch", annotations=PAID)
async def linkup_fetch(
    url: Annotated[str, Field(description="HTTP or HTTPS URL of an HTML page (up to 20 MB) or PDF (up to 100 MB). No login walls, no LinkedIn (use linkup-search for LinkedIn).")],
    render_js: Annotated[bool, Field(description="Render JavaScript before extraction. Use true for single-page apps or when the markdown comes back short or says 'Loading'. Raises the price from 0.001 to 0.005 (standard) or 0.005 to 0.01 (pro).")] = False,
    mode: Annotated[Literal["standard", "pro"], Field(description="pro has a much higher success rate on hard-to-retrieve pages (anti-bot), at 0.005 or 0.01 per call instead of 0.001 or 0.005. Independent of render_js.")] = "standard",
    schema: Annotated[JsonSchema, Field(description="Optional JSON Schema (root type object) to extract typed JSON from this page into the data field. Fields not found on the page are omitted, even if required. Adds 0.001.")] = None,
    instructions: Annotated[str | None, Field(description="Global extraction rules for schema fill, up to 4000 characters (for example 'public list prices only, in USD'). Requires schema.", max_length=4000)] = None,
    include_raw_content: Annotated[bool, Field(description="Also return rawContent (raw page bytes as text) and contentType. Large.")] = False,
    extract_images: Annotated[bool, Field(description="Also return the list of images (alt, url). Adds latency.")] = False,
    max_chars: Annotated[int, Field(description="Cap on the size of the returned JSON; the markdown (and rawContent) are truncated to fit and _truncated is set.", ge=2000)] = 40000,
) -> dict[str, Any]:
    """Fetch one web page as clean markdown with Linkup (POST /v1/fetch, synchronous).

    Price per call: standard 0.001 (no JS) or 0.005 (render_js); pro 0.005 or 0.01; a schema
    adds 0.001. Returns markdown and favicon always; data with a schema; images with
    extract_images; rawContent and contentType with include_raw_content. The endpoint is
    anonymous, reads this one URL only and follows no links. HTTP 400 is returned for binary
    files other than PDF, unreachable targets, oversize pages, instructions without schema,
    and failed structured extraction; such calls are not billed.
    """
    if instructions and schema is None:
        raise ToolError("instructions requires schema (Linkup returns HTTP 400 otherwise).")
    body = _drop_none(
        {
            "url": url,
            "renderJs": render_js,
            "mode": mode,
            "schema": _schema_as_object(schema, "schema"),
            "instructions": instructions,
            "includeRawContent": include_raw_content or None,
            "extractImages": extract_images or None,
        }
    )
    data = await _request("POST", "/fetch", json_body=body)
    out = {"result": data, "cost_usd_estimate": fetch_price(mode, render_js, schema is not None)}
    return _finish(out, max_chars)


# --------------------------------------------------------------------------
# Research (asynchronous agent)
# --------------------------------------------------------------------------
RESEARCH_DOC = """Start an autonomous research task with Linkup (POST /v1/research). Asynchronous:
    returns at once with id and status "pending"; the agent then plans, searches many sources in
    parallel, verifies and synthesises, in 2 to 20 minutes. Collect the result with
    linkup-wait-research (polls for you) or linkup-get-research.

    Price per task by reasoning_depth: S 0.25 USD (2 to 5 min), M 0.50 (3 to 7 min),
    L 1.50 (5 to 10 min, the default), XL 2.50 (10 to 20 min). Failed tasks are not billed.
    Do not create a second task to "retry" a slow one; wait on the id you have.

    Use it for questions a single search cannot resolve: verified answers with an audit trail
    of citations, a focused investigation of one subject, or a broad report by theme. For a
    quick fact use linkup-search. Set mode explicitly for predictable latency, cost and
    shape. Phrase the question precisely: angles to cover, leads to pursue, facts to verify,
    entities to compare, constraints, and the structure you expect back. Put dates in
    from_date/to_date, give a few trusted include_domains when they exist.
"""


@tool(name="linkup-research", title="Linkup deep research (start task)", annotations=CREATES, description=RESEARCH_DOC)
async def linkup_research(
    query: Annotated[str, Field(description="The research question, precise and complete (see tool description).", min_length=1)],
    mode: Annotated[Literal["answer", "investigate", "research"] | None, Field(description="answer: precise, evidence-backed answer with a definitive solution. investigate: focused report on one defined subject. research: structured report by theme over many topics or entities in parallel. Omit (API default auto) to let the agent classify the question.")] = None,
    reasoning_depth: Annotated[Literal["S", "M", "L", "XL"], Field(description="S light, M balanced, L thorough (default), XL exhaustive. Higher depth does not mean a longer answer, it means more coverage.")] = "L",
    output_type: Annotated[Literal["sourcedAnswer", "structured"], Field(description="sourcedAnswer: natural-language answer with citations and a sources list. structured: JSON matching structured_output_schema (keep it flat; nested arrays of objects raise latency and failure rate).")] = "sourcedAnswer",
    structured_output_schema: Annotated[JsonSchema, Field(description="Required when output_type is structured. JSON Schema (object or string), root type object.")] = None,
    include_domains: Annotated[list[str] | None, Field(description="Restrict sources to these domains (up to 100). Strongly recommended when authoritative sources are known: better quality, lower latency.")] = None,
    exclude_domains: Annotated[list[str] | None, Field(description="Domains to exclude.")] = None,
    from_date: Annotated[str | None, Field(description="YYYY-MM-DD, sources on or after this date.")] = None,
    to_date: Annotated[str | None, Field(description="YYYY-MM-DD, sources on or before this date.")] = None,
) -> dict[str, Any]:
    if output_type == "structured" and structured_output_schema is None:
        raise ToolError("output_type 'structured' needs structured_output_schema.")
    if include_domains and len(include_domains) > 100:
        raise ToolError("include_domains takes at most 100 domains.")
    body = _drop_none(
        {
            "q": query,
            "outputType": output_type,
            "mode": mode,
            "reasoningDepth": reasoning_depth,
            "structuredOutputSchema": _schema_as_string(structured_output_schema, "structured_output_schema"),
            "includeDomains": include_domains,
            "excludeDomains": exclude_domains,
            "fromDate": from_date,
            "toDate": to_date,
        }
    )
    data = await _request("POST", "/research", json_body=body)
    return {
        "task": data,
        "cost_usd_estimate": RESEARCH_PRICE[reasoning_depth],
        "next": "Call linkup-wait-research with this id (or linkup-get-research to poll yourself). Typical completion: "
        + {"S": "2 to 5", "M": "3 to 7", "L": "5 to 10", "XL": "10 to 20"}[reasoning_depth]
        + " minutes.",
    }


@tool(name="linkup-get-research", title="Linkup research result (poll once)", annotations=RO)
async def linkup_get_research(
    id: Annotated[str, Field(description="Task id returned by linkup-research (or a research task from linkup-tasks-create).", min_length=1)],
    max_chars: Annotated[int, Field(description="Cap on the returned JSON size.", ge=2000)] = 48000,
) -> dict[str, Any]:
    """Read one research task (GET /v1/research/{id}). Free.

    status is pending, processing, completed or failed. When completed, output holds the
    answer and sources (sourcedAnswer) or the JSON object (structured); when failed, error
    holds the reason. Poll at 2 s backing off to 10 s and never faster than once per second,
    or use linkup-wait-research, which does that for you.
    """
    data = await _request("GET", f"/research/{id}")
    return _finish({"task": data}, max_chars)


@tool(name="linkup-wait-research", title="Linkup research result (wait)", annotations=RO)
async def linkup_wait_research(
    id: Annotated[str, Field(description="Task id from linkup-research.", min_length=1)],
    max_wait_seconds: Annotated[int, Field(description="How long this call may wait before returning the current state. Call again with the same id to keep waiting; nothing is re-created or re-billed.", ge=5, le=1500)] = 600,
    max_chars: Annotated[int, Field(description="Cap on the returned JSON size.", ge=2000)] = 48000,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Wait for a research task and return it when it is completed or failed (GET /v1/research/{id} in a loop).

    Polls at Linkup's recommended cadence: 2 s, doubling up to 10 s, never above 1 request
    per second, and reports progress to the client. If max_wait_seconds elapses first, the
    task is returned with its current status (pending or processing) and done=false: call
    this tool again with the same id. Free; the task itself was billed when created.
    """
    start = time.monotonic()
    interval = 2.0
    polls = 0
    while True:
        data = await _request("GET", f"/research/{id}")
        polls += 1
        status = data.get("status") if isinstance(data, dict) else None
        elapsed = time.monotonic() - start
        if ctx is not None:
            try:
                await ctx.report_progress(min(elapsed, max_wait_seconds), max_wait_seconds, f"research {id}: {status} after {int(elapsed)} s ({polls} polls)")
            except Exception:
                pass
        if status in ("completed", "failed"):
            return _finish({"done": True, "waited_seconds": round(elapsed), "polls": polls, "task": data}, max_chars)
        if elapsed + interval > max_wait_seconds:
            return {
                "done": False,
                "waited_seconds": round(elapsed),
                "polls": polls,
                "task": data,
                "next": "Still running. Call linkup-wait-research again with the same id.",
            }
        await asyncio.sleep(interval)
        interval = min(interval * 2, 10.0)


@tool(name="linkup-list-research", title="Linkup research tasks (list)", annotations=RO)
async def linkup_list_research(
    page: Annotated[int, Field(description="Page number, from 1.", ge=1)] = 1,
    page_size: Annotated[int, Field(description="Tasks per page, 1 to 100.", ge=1, le=100)] = 10,
    sort_by: Annotated[Literal["createdAt", "updatedAt"] | None, Field(description="Sort field.")] = None,
    sort_direction: Annotated[Literal["asc", "desc"] | None, Field(description="API default asc; use desc for newest first.")] = "desc",
    include_output: Annotated[bool, Field(description="true: include each task's full output (large). Default: a short output_preview per task; read one task with linkup-get-research.")] = False,
    max_chars: Annotated[int, Field(description="Cap on the returned JSON size.", ge=2000)] = 40000,
) -> dict[str, Any]:
    """List the organisation's research tasks (GET /v1/research). Free.

    Returns data (tasks, each with id, status, input, error, createdAt, updatedAt and an
    output_preview, or the full output with include_output) and metadata (page, pageSize,
    total, totalPages). Results are kept for a bounded period; store what you need.
    """
    data = await _request("GET", "/research", params=_page_params(page, page_size, sort_by, sort_direction))
    return _finish({"result": _summarize_tasks(data, include_output)}, max_chars)


# --------------------------------------------------------------------------
# Tasks (asynchronous batch wrapper)
# --------------------------------------------------------------------------
TASK_TYPES = ("search", "fetch", "research", "extract")


def _estimate_task(task: dict[str, Any]) -> float | None:
    t, inp = task.get("type"), task.get("input") or {}
    if t == "search":
        return search_price(str(inp.get("depth", "fast")), str(inp.get("outputType", "searchResults")))
    if t == "fetch":
        return fetch_price(str(inp.get("mode", "standard")), bool(inp.get("renderJs", False)), inp.get("schema") is not None)
    if t == "research":
        return RESEARCH_PRICE.get(str(inp.get("reasoningDepth", "L")), 1.5)
    return None  # extract: variable, 2 to 10 USD typical


@tool(name="linkup-tasks-create", title="Linkup batch tasks (create)", annotations=CREATES)
async def linkup_tasks_create(
    tasks: Annotated[list[dict[str, Any]], Field(description='1 to 100 items, each {"type": "search" | "fetch" | "research" | "extract", "input": {...}} where input has exactly the fields of the matching endpoint (camelCase: q, depth, outputType, url, renderJs, mode, reasoningDepth, includeDomains, fromDate ...). Types can be mixed. Tasks run in parallel; submit dependent work (fetch after search) as a second batch.', min_length=1, max_length=100)],
    dry_run: Annotated[bool, Field(description="true: validate and return the cost estimate without submitting anything.")] = False,
) -> dict[str, Any]:
    """Create a batch of asynchronous tasks (POST /v1/tasks). Each task is billed exactly like the
    direct call: search 0.005 to 0.055, fetch 0.001 to 0.011, research 0.25 to 2.50 (100 research
    tasks at the default depth L cost 150 USD), extract variable (2 to 10 typical). There is no
    batch discount; Tasks is for bulk, long-running or mixed work, not for single calls.

    Returns the task envelopes (id, type, status pending, input, output null). Then poll with
    linkup-tasks-list (bulk, every 5 s for research, 1 to 2 s for search and fetch) or
    linkup-tasks-get (one task). A failing task does not fail the batch and is not billed.
    The organisation has a concurrency quota (see quota in linkup-tasks-list); above it the API
    answers 429. Results are kept for a bounded period: store them when they complete.
    """
    norm: list[dict[str, Any]] = []
    for i, t in enumerate(tasks):
        if not isinstance(t, dict) or t.get("type") not in TASK_TYPES or not isinstance(t.get("input"), dict):
            raise ToolError(f'tasks[{i}] must be {{"type": one of {TASK_TYPES}, "input": {{...}}}}.')
        inp = dict(t["input"])
        if t["type"] in ("search", "research") and isinstance(inp.get("structuredOutputSchema"), dict):
            inp["structuredOutputSchema"] = json.dumps(inp["structuredOutputSchema"])
        if t["type"] == "search":
            for req in ("q", "depth", "outputType"):
                if req not in inp:
                    raise ToolError(f"tasks[{i}] (search) needs {req}.")
        if t["type"] == "research" and ("q" not in inp or "outputType" not in inp):
            raise ToolError(f"tasks[{i}] (research) needs q and outputType.")
        if t["type"] == "fetch" and "url" not in inp:
            raise ToolError(f"tasks[{i}] (fetch) needs url.")
        if t["type"] == "extract" and ("q" not in inp or "url" not in inp):
            raise ToolError(f"tasks[{i}] (extract) needs q and url.")
        norm.append({"type": t["type"], "input": inp})
    estimates = [_estimate_task(t) for t in norm]
    known = sum(e for e in estimates if e is not None)
    unknown = sum(1 for e in estimates if e is None)
    estimate = {"usd_known": round(known, 4), "tasks_with_variable_price": unknown, "by_type": {k: sum(1 for t in norm if t["type"] == k) for k in TASK_TYPES if any(t["type"] == k for t in norm)}}
    if dry_run:
        return {"dry_run": True, "would_submit": len(norm), "cost_estimate": estimate}
    data = await _request("POST", "/tasks", json_body=norm)
    ids = [t.get("id") for t in data] if isinstance(data, list) else None
    return {"submitted": len(norm), "cost_estimate": estimate, "task_ids": ids, "tasks": data}


@tool(name="linkup-tasks-get", title="Linkup task (get)", annotations=RO)
async def linkup_tasks_get(
    id: Annotated[str, Field(description="Task id (search, fetch, research or extract task).", min_length=1)],
    max_chars: Annotated[int, Field(description="Cap on the returned JSON size.", ge=2000)] = 48000,
) -> dict[str, Any]:
    """Read one task of any type (GET /v1/tasks/{id}). Free. status: pending, processing,
    completed, failed. output has the same shape as the matching synchronous endpoint; for
    extract tasks it holds resultUrl (download with linkup-extract-download)."""
    data = await _request("GET", f"/tasks/{id}")
    return _finish({"task": data}, max_chars)


@tool(name="linkup-tasks-list", title="Linkup tasks (list)", annotations=RO)
async def linkup_tasks_list(
    type: Annotated[list[Literal["search", "fetch", "research", "extract"]] | None, Field(description="Filter by task type(s).")] = None,
    status: Annotated[list[Literal["pending", "processing", "completed", "failed"]] | None, Field(description="Filter by status(es).")] = None,
    page: Annotated[int, Field(ge=1)] = 1,
    page_size: Annotated[int, Field(description="1 to 100.", ge=1, le=100)] = 20,
    sort_by: Annotated[Literal["createdAt", "updatedAt"] | None, Field()] = None,
    sort_direction: Annotated[Literal["asc", "desc"] | None, Field(description="API default asc; desc for newest first.")] = "desc",
    include_output: Annotated[bool, Field(description="true: include each task's full output (large). Default: a short output_preview per task; read one task with linkup-tasks-get.")] = False,
    max_chars: Annotated[int, Field(description="Cap on the returned JSON size.", ge=2000)] = 40000,
) -> dict[str, Any]:
    """List the organisation's tasks of all types (GET /v1/tasks). Free. Returns data (task
    envelopes with an output_preview, or full outputs with include_output), metadata (page,
    pageSize, total, totalPages) and quota (inFlight: tasks pending or processing now; limit:
    maximum concurrent tasks allowed). Prefer this bulk poll over many linkup-tasks-get calls;
    the poll cap is 1 request per second."""
    params = _page_params(page, page_size, sort_by, sort_direction)
    if type:
        params["type"] = type
    if status:
        params["status"] = status
    data = await _request("GET", "/tasks", params=params)
    return _finish({"result": _summarize_tasks(data, include_output)}, max_chars)


# --------------------------------------------------------------------------
# Credits
# --------------------------------------------------------------------------
@tool(name="linkup-credits-balance", title="Linkup credit balance", annotations=RO)
async def linkup_credits_balance() -> dict[str, Any]:
    """Return the prepaid balance in USD (GET /v1/credits/balance). Free. Linkup credits
    eligible accounts back up to 20 USD a month; when the balance is used up the API answers
    429. Extract tasks need a balance of at least 10 USD."""
    data = await _request("GET", "/credits/balance")
    return {"result": data, "currency": "USD"}


# --------------------------------------------------------------------------
# Extract (closed beta)
# --------------------------------------------------------------------------
@tool(name="linkup-extract", title="Linkup extract (closed beta, start task)", annotations=CREATES)
async def linkup_extract(
    query: Annotated[str, Field(description="What rows to extract and what each row holds, in natural language.", min_length=1)],
    url: Annotated[str, Field(description="Seed URL the agent starts from (listing, directory, catalogue page).")],
    schema: Annotated[JsonSchema, Field(description="Optional JSON Schema (root type object) describing ONE row; applied to every row.")] = None,
    verify_urls: Annotated[bool, Field(description="Check that URLs in the rows resolve. Adds latency.")] = False,
) -> dict[str, Any]:
    """Start an Extract task (POST /v1/extract) that turns a listing page into rows of data,
    delivered as an NDJSON file. CLOSED BETA: the API answers 403 unless Linkup has enabled it
    for the organisation. Variable price, typically 2 to 10 USD per completed task, shown as
    creditsUsed in the output; failed tasks are free; a balance of at least 10 USD is required.
    Asynchronous: poll with linkup-get-extract every 30 s, then download with
    linkup-extract-download. For one JSON object from one known page use linkup-fetch with
    schema instead."""
    body = _drop_none({"q": query, "url": url, "schema": _schema_as_object(schema, "schema"), "verifyUrls": verify_urls or None})
    data = await _request("POST", "/extract", json_body=body)
    return {"task": data, "next": "Poll linkup-get-extract every 30 s; when completed, call linkup-extract-download with the id."}


@tool(name="linkup-get-extract", title="Linkup extract task (get)", annotations=RO)
async def linkup_get_extract(id: Annotated[str, Field(min_length=1)]) -> dict[str, Any]:
    """Read one extract task (GET /v1/extract/{id}). Free. When completed, output holds
    creditsUsed, rowsReturned and resultUrl (an NDJSON download link valid for 24 hours); the
    rows are not in the response."""
    data = await _request("GET", f"/extract/{id}")
    return {"task": data}


@tool(name="linkup-list-extract", title="Linkup extract tasks (list)", annotations=RO)
async def linkup_list_extract(
    page: Annotated[int, Field(ge=1)] = 1,
    page_size: Annotated[int, Field(ge=1, le=100)] = 10,
    sort_by: Annotated[Literal["createdAt", "updatedAt"] | None, Field()] = None,
    sort_direction: Annotated[Literal["asc", "desc"] | None, Field()] = "desc",
) -> dict[str, Any]:
    """List extract tasks (GET /v1/extract). Free. Closed beta."""
    data = await _request("GET", "/extract", params=_page_params(page, page_size, sort_by, sort_direction))
    return {"result": data}


@tool(name="linkup-extract-download", title="Linkup extract rows (download)", annotations=RO)
async def linkup_extract_download(
    id: Annotated[str, Field(description="Completed extract task id.", min_length=1)],
    max_rows: Annotated[int, Field(description="Maximum rows to return.", ge=1, le=5000)] = 200,
    max_chars: Annotated[int, Field(description="Cap on the returned JSON size.", ge=2000)] = 48000,
) -> dict[str, Any]:
    """Download the NDJSON result of a completed extract task and return its rows as JSON.
    Free (the download link in the task output is a plain file URL, valid 24 hours)."""
    task = await _request("GET", f"/extract/{id}")
    if not isinstance(task, dict) or task.get("status") != "completed" or not (task.get("output") or {}).get("resultUrl"):
        raise ToolError(f"Extract task {id} is not completed or has no resultUrl: status={task.get('status') if isinstance(task, dict) else task}.")
    url = task["output"]["resultUrl"]
    try:
        async with httpx.AsyncClient(timeout=120.0) as dl:
            resp = await dl.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolError(f"Download of the extract result failed: {exc}") from exc
    rows: list[Any] = []
    total = 0
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        total += 1
        if len(rows) < max_rows:
            try:
                rows.append(json.loads(line))
            except ValueError:
                rows.append({"_unparsed": line})
    return _finish({"id": id, "rows_in_file": total, "rows_returned": len(rows), "creditsUsed": task["output"].get("creditsUsed"), "rows": rows}, max_chars)


# --------------------------------------------------------------------------
# Rewind (closed beta)
# --------------------------------------------------------------------------
@tool(name="linkup-rewind-search", title="Linkup rewind search (beta)", annotations=PAID)
async def linkup_rewind_search(
    query: Annotated[str, Field(min_length=1)],
    as_of: Annotated[str, Field(description="UTC day to search as of, YYYY-MM-DD.")],
    max_chars: Annotated[int, Field(ge=2000)] = 40000,
) -> dict[str, Any]:
    """Search the web as it was at the end of a given UTC day (POST /v1/rewind/search). CLOSED
    BETA: 403 unless enabled for the organisation. Returns results with content, name, url,
    validFrom and validTo (the first day the revision was no longer visible, null if current)."""
    data = await _request("POST", "/rewind/search", json_body={"q": query, "asOf": as_of})
    return _finish({"result": data}, max_chars)


@tool(name="linkup-rewind-fetch", title="Linkup rewind fetch (beta)", annotations=PAID)
async def linkup_rewind_fetch(
    url: Annotated[str, Field(description="Exact stored URL, no normalisation is applied.")],
    as_of: Annotated[str, Field(description="UTC day, YYYY-MM-DD.")],
    max_chars: Annotated[int, Field(ge=2000)] = 40000,
) -> dict[str, Any]:
    """Return a page as it was at the end of a given UTC day (POST /v1/rewind/fetch). CLOSED
    BETA: 403 unless enabled; 404 when no revision is visible that day; 422 when the day is
    not available."""
    data = await _request("POST", "/rewind/fetch", json_body={"url": url, "asOf": as_of})
    return _finish({"result": data}, max_chars)


# --------------------------------------------------------------------------
# Responses (OpenAI-style proxy)
# --------------------------------------------------------------------------
@tool(name="linkup-responses", title="Linkup responses (OpenAI-style)", annotations=PAID)
async def linkup_responses(
    input: Annotated[str | list[dict[str, str]], Field(description='A question string, or a list of messages [{"role": "user" | "assistant" | "developer", "content": "..."}].')],
    model: Annotated[Literal["linkup-standard", "linkup-deep"], Field(description="linkup-standard answers faster; linkup-deep takes longer and is more comprehensive.")] = "linkup-standard",
    instructions: Annotated[str | None, Field(description="System-message-like guidance for the answer.")] = None,
    text_format: Annotated[JsonSchema, Field(description="Optional JSON Schema (root type object) for structured text output; omit for plain text.")] = None,
    max_chars: Annotated[int, Field(ge=2000)] = 40000,
) -> dict[str, Any]:
    """Generate a web-grounded answer in the OpenAI Responses format (POST /v1/responses).
    This is the endpoint behind using Linkup as an OpenAI-compatible base URL. Returns
    output_text and output messages with url_citation annotations. Its price is not published
    on the pricing page (read 2026-11-13); check linkup-credits-balance before and after if it
    matters. For most uses linkup-search with sourcedAnswer is the simpler call."""
    body: dict[str, Any] = {"input": input, "model": model}
    if instructions:
        body["instructions"] = instructions
    if text_format is not None:
        body["text"] = {"format": _schema_as_object(text_format, "text_format")}
    data = await _request("POST", "/responses", json_body=body)
    return _finish({"result": data}, max_chars)


# --------------------------------------------------------------------------
# Guide (documentation inside the server)
# --------------------------------------------------------------------------
GUIDE: dict[str, str] = {
    "overview": f"""# Linkup API, as read on {DOCS_READ_DATE}

Base URL https://api.linkup.so/v1, auth header "Authorization: Bearer <key>", JSON bodies.
Billing: prepaid USD balance (20 USD free for professional e-mail sign-ups, topped back up
monthly for eligible accounts); failed or empty calls are not billed; when the balance is used
up the API answers 429. x402 pay-per-request in USDC also exists (not used here).

| Endpoint | Tool(s) | Sync? | Price (USD/call) | Latency |
|---|---|---|---|---|
| POST /search | linkup-search | yes | 0.005 to 0.055 | 0.2 s (flash) to 30 s (deep) |
| POST /fetch | linkup-fetch | yes | 0.001 to 0.011 | seconds |
| POST /research, GET /research, GET /research/{{id}} | linkup-research, linkup-wait-research, linkup-get-research, linkup-list-research | no | 0.25 (S), 0.50 (M), 1.50 (L), 2.50 (XL) | 2 to 20 min |
| POST /tasks, GET /tasks, GET /tasks/{{id}} | linkup-tasks-create, linkup-tasks-list, linkup-tasks-get | no | same as the wrapped call | same |
| GET /credits/balance | linkup-credits-balance | yes | free | |
| POST /extract, GET /extract, GET /extract/{{id}} (closed beta) | linkup-extract, linkup-list-extract, linkup-get-extract, linkup-extract-download | no | 2 to 10 typical, variable | minutes |
| POST /rewind/search, POST /rewind/fetch (closed beta) | linkup-rewind-search, linkup-rewind-fetch | yes | not published | |
| POST /responses | linkup-responses | yes | not published | |

Rate limits: 10 requests per second per organisation on search and fetch; polling of tasks
and research at most 1 request per second. Errors carry {{statusCode, error: {{code, message,
details[]}}}}. Docs index: https://docs.linkup.so/llms.txt. OpenAPI: https://api.linkup.so/v1/openapi.json
(snapshot in resource linkup://openapi.json).

Choosing: a known URL -> fetch; a fact or a list of sources -> search (fast by default,
standard for instructions or breadth, deep for multi-step); synthesis across many sources
with citations -> research; hundreds of calls or long jobs -> tasks.
""",
    "search": """# /search (linkup-search)

Parameters: q (required), depth (flash | fast | standard | deep, required), outputType
(searchResults | sourcedAnswer | structured, required), structuredOutputSchema (JSON schema as
a string, root object, required for structured), includeImages, includeInlineCitations
(sourcedAnswer only), includeSources (structured only; response becomes {data, sources}),
maxResults (>= 1), includeDomains (<= 100), excludeDomains, fromDate, toDate (YYYY-MM-DD).

Depths: flash (alpha) under 200 ms, keyword-only, no LLM; fast about 1 s, keyword-only, the
recommended default; standard 1 to 3 s, one agentic pass, interprets instructions, can fan out
to parallel sub-searches when told ("run several searches with adjacent keywords") and scrape
one URL given in the query; deep 5 to 30 s, several iterations, follows ordered steps ("First
find X. Then scrape it. Then return Y"), scrapes several URLs with JavaScript rendering.

Prices: flash/fast/standard 0.005 (searchResults) or 0.006 (sourcedAnswer, structured);
deep 0.05 or 0.055.

Responses: searchResults -> {results: [{type: "text", name, url, content, favicon,
publishedAt?} | {type: "image", name, url}]}; sourcedAnswer -> {answer, sources: [{name,
url, snippet, favicon, publishedAt?}]}; structured -> your object, or {data, sources} with
includeSources.

Practice: say what to retrieve, then how to use it; keep flash/fast queries keyword-shaped;
dates go in fromDate/toDate; a few includeDomains improve quality; LinkedIn profile, company
and post pages are readable only through search with the exact URL in the query (standard or
deep), never through fetch. Output type: searchResults when an LLM reasons over the sources,
sourcedAnswer for display, structured for code without a known URL, fetch with schema for
code with a known URL. Prompt rewriting help: https://prompt.linkup.so
""",
    "fetch": """# /fetch (linkup-fetch)

Parameters: url (required, http/https, HTML <= 20 MB or PDF <= 100 MB), mode (standard |
pro, default standard), renderJs (default false), includeRawContent (raw content plus
contentType), includeRawHtml (deprecated), extractImages, schema (JSON schema object, root
object; adds data), instructions (<= 4000 chars, requires schema).

Prices: standard 0.001 (no JS) or 0.005 (renderJs); pro 0.005 or 0.01; schema adds 0.001.

Response: {markdown, favicon, contentType?, rawContent?, rawHtml?, images?: [{alt, url}],
data?}. Anonymous (login walls return the public view), reads this URL only, follows no
links, no LinkedIn. renderJs true is the safer default in agent pipelines; signs it is needed:
short markdown, "Loading...", missing sections. 400 for other binaries, unreachable or
oversize targets, instructions without schema, non-object schema, failed extraction; none
billed. For many rows from a listing page use extract (closed beta).
""",
    "research": """# /research (linkup-research, linkup-wait-research, linkup-get-research, linkup-list-research)

An autonomous agent for questions a single search cannot resolve: it plans, runs searches in
parallel, iterates, verifies and returns a sourced answer with inline citations.

POST /research body: q (required), outputType (sourcedAnswer | structured, required),
structuredOutputSchema (string, root object; required for structured; keep flat), mode
(answer | auto | investigate | research; API default auto), reasoningDepth (S | M | L | XL,
default L), includeDomains (<= 100, strongly recommended), excludeDomains, fromDate, toDate.
Modes: answer = precise, evidence-backed answer with a definitive solution (cross-checks);
investigate = focused report on one defined subject; research = structured report by theme
over many topics or entities in parallel. Set mode explicitly for predictable latency, cost
and shape.

Depth, price, latency: S 0.25 USD, 2 to 5 min; M 0.50, 3 to 7 min; L 1.50, 5 to 10 min
(default); XL 2.50, 10 to 20 min. More depth means more coverage, not a longer text.
Failed tasks and tasks with no result are free; retries are unrestricted.

Lifecycle: POST returns {id, type: "research", status: "pending", input, output: null,
error: null, createdAt, updatedAt}. GET /research/{id} returns the same envelope; status
pending -> processing -> completed | failed; output = {answer, sources: [{name, url, snippet,
favicon, publishedAt?}]} or the structured object; error = reason when failed.
GET /research lists tasks (page, pageSize <= 100, sortBy, sortDirection).

Polling: 2 s, doubling to a 10 s cap, at most 1 request per second (Linkup rate-limits
faster polling without finishing sooner). linkup-wait-research does exactly this, time-boxed,
and can be called again with the same id. Results are kept for a bounded period.

Phrasing: name the angles, leads, facts to verify, entities to compare, constraints and the
output structure. Dates in fromDate/toDate, not in the text.
Pages: research/overview, research/best-practices, research/for-agents, research/post,
research/get, research/list under https://docs.linkup.so/pages/documentation/endpoints/
""",
    "tasks": """# /tasks (linkup-tasks-create, linkup-tasks-list, linkup-tasks-get)

Asynchronous batch wrapper around search, fetch, research and extract: POST /tasks with a JSON
array of 1 to 100 {type, input} items (mixed types allowed; input = the same fields as the
direct endpoint). Returns the envelopes at once with status pending. GET /tasks lists with
filters type[], status[], page, pageSize (<= 100), sortBy, sortDirection and returns
{metadata: {page, pageSize, total, totalPages}, data: [...], quota: {inFlight, limit}}.
GET /tasks/{id} reads one task; output has the direct endpoint's shape when completed.

Cost: each task is billed exactly like the direct call; no discount, no surcharge. Use tasks
for bulk work, long jobs, scheduled pipelines, mixed batches, or when the synchronous
concurrency budget is exceeded; not for single interactive calls. Tasks run in parallel and
in no fixed order; submit dependent work as a second batch. Each task completes or fails on
its own; failures are free; retry only the failures. Poll cadence: search/fetch batches 1 to
2 s, research batches 5 s backing off to 30 s, mixed 2 s to 10 s, never above 1 request per
second; bulk polling via GET /tasks uses fewer calls than per-task polling. Results have a
bounded lifetime: persist them when complete. The concurrency limit is reported as quota.limit.
""",
    "pricing": """# Pricing and limits (read 2026-11-13)

Search: flash/fast/standard with searchResults 0.005 USD; with sourcedAnswer or structured
0.006; deep 0.05 and 0.055.
Fetch: standard 0.001 (renderJs false) or 0.005 (true); pro 0.005 or 0.01; schema +0.001.
Research: S 0.25, M 0.50, L 1.50, XL 2.50 per task.
Extract (closed beta): variable, mostly 2 to 10 USD, reported as creditsUsed; needs a balance
of at least 10 USD.
Tasks: same as the wrapped call. x402: same prices, minimum 0.01 per request.
Free sign-up credit 20 USD (professional e-mail), topped back up monthly for eligible
accounts; a top-up of 1000 USD or more adds free credits. No charge on errors, including
"no result". Balance: GET /credits/balance, app: https://app.linkup.so/organization/billing

Rate limits: search and fetch 10 requests per second per organisation (x402: per IP); task
and research polling at most 1 request per second; tasks: 100 per submission, concurrency
quota per organisation. Higher limits: contact@linkup.so.

Errors: {statusCode, error: {code, message, details: [{field, message}]}}. 400 bad request or
no result; 401 bad key; 402 x402 payment; 403 no permission (betas); 409 conflict; 429 out of
credit or too many concurrent requests; 500 Linkup error; 504 deadline exceeded.
""",
    "extract": """# /extract (closed beta; linkup-extract, linkup-get-extract, linkup-list-extract, linkup-extract-download)

Turns a listing page into rows: body {q (required: which rows, which fields), url (required
seed URL), schema (JSON schema of ONE row, optional), verifyUrls (default false)}. Returns a
task envelope (status pending); GET /extract/{id} until completed; output = {creditsUsed,
resultUrl (NDJSON download, valid 24 hours), rowsReturned}; the rows are only in the file.
Poll every 30 s. Variable price, mostly 2 to 10 USD, failed tasks free, balance >= 10 USD
required. 403 unless Linkup enabled the beta for the organisation (request access:
contact@linkup.so). For one object from one page use fetch with schema.

# /rewind (closed beta; linkup-rewind-search, linkup-rewind-fetch)
POST /rewind/search {q, asOf: YYYY-MM-DD} returns pages as visible at the end of that UTC
day: {asOf, results: [{content, name, url, validFrom, validTo | null}]}. POST /rewind/fetch
{url (exact stored URL), asOf} returns one revision {asOf, content, name, url, validFrom,
validTo}. 403 unless enabled, 404 no revision that day, 422 day not available.

# /responses (linkup-responses)
OpenAI Responses-format proxy: {input (string or messages), model (linkup-standard |
linkup-deep), instructions?, text: {format: "text" | json-schema}}; returns output_text and
messages with url_citation annotations.
""",
}
GUIDE["links"] = """# Links
- Docs index: https://docs.linkup.so/llms.txt
- Search: https://docs.linkup.so/pages/documentation/endpoints/search/overview and .../search/best-practices
- Fetch: https://docs.linkup.so/pages/documentation/endpoints/fetch/overview and .../fetch/best-practices
- Research: https://docs.linkup.so/pages/documentation/endpoints/research/overview, .../research/best-practices, .../research/for-agents
- Tasks: https://docs.linkup.so/pages/documentation/endpoints/tasks/overview and .../tasks/best-practices
- Extract (beta): https://docs.linkup.so/pages/documentation/endpoints/extract/overview
- Pricing: https://docs.linkup.so/pages/documentation/platform/pricing
- Rate limits: https://docs.linkup.so/pages/documentation/platform/rate-limits
- Errors: https://docs.linkup.so/pages/documentation/platform/errors
- Structured output: https://docs.linkup.so/pages/documentation/tutorials/structured-output-guide
- Filtering: https://docs.linkup.so/pages/documentation/tutorials/filtering
- OpenAPI: https://api.linkup.so/v1/openapi.json
- Official MCP server (4 tools): https://github.com/LinkupPlatform/linkup-mcp-server, hosted at https://mcp.linkup.so/mcp
- Playground: https://app.linkup.so/playground, billing: https://app.linkup.so/organization/billing
"""


@tool(name="linkup-guide", title="Linkup API guide", annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def linkup_guide(
    topic: Annotated[Literal["overview", "search", "fetch", "research", "tasks", "pricing", "extract", "links", "all"], Field(description="Which part of the guide to return.")] = "overview",
) -> str:
    """Explain the Linkup API: endpoints, parameters, prices, latencies, limits, polling rules and
    best practices, condensed from Linkup's documentation (read 2026-11-13). Free, no network."""
    if topic == "all":
        return "\n\n".join(GUIDE[k] for k in ("overview", "search", "fetch", "research", "tasks", "pricing", "extract", "links"))
    return GUIDE[topic]


@mcp.resource("linkup://guide", name="Linkup API guide", mime_type="text/markdown")
def guide_resource() -> str:
    """The full Linkup API guide (same text as linkup-guide with topic all)."""
    return "\n\n".join(GUIDE[k] for k in ("overview", "search", "fetch", "research", "tasks", "pricing", "extract", "links"))


@mcp.resource("linkup://openapi.json", name="Linkup OpenAPI snapshot", mime_type="application/json")
def openapi_resource() -> str:
    """Snapshot of https://api.linkup.so/v1/openapi.json taken on 2026-11-13."""
    if OPENAPI_SNAPSHOT.exists():
        return OPENAPI_SNAPSHOT.read_text(encoding="utf-8")
    return json.dumps({"error": "openapi.json snapshot missing next to server.py", "live": "https://api.linkup.so/v1/openapi.json"})


def main() -> None:
    """Entry point: run the server on stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
