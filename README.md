# linkup-mcp

An MCP server for the whole [Linkup](https://www.linkup.so) web API, one tool per
operation, with Linkup's own operational guidance (prices, latencies, limits, polling
rules, best practices) in the tool descriptions and in a built-in guide. Stdio transport,
Python, two dependencies.

Not affiliated with Linkup. Written against `https://api.linkup.so/v1/openapi.json` and the
documentation as read on 2026-11-13; the snapshot of the spec is shipped as a resource.

## Why

Linkup's official server ([`linkup-mcp-server`](https://github.com/LinkupPlatform/linkup-mcp-server),
also hosted at `https://mcp.linkup.so/mcp`) exposes four tools: `linkup-search` (always a
sourced answer, no output type), `linkup-research` (output fixed to `sourcedAnswer`),
`linkup-get-research` and `linkup-fetch` (no pro mode, no schema extraction). It has no
research listing, no batch tasks, no credit balance, no extract, rewind or responses. This
server keeps those four tool names, gives them every documented parameter, and adds the rest.

## Tools

| Tool | API | Billed per call (USD) |
|---|---|---|
| linkup-search | POST /search | 0.005 to 0.055 |
| linkup-fetch | POST /fetch | 0.001 to 0.011 |
| linkup-research | POST /research (create task) | 0.25 (S), 0.50 (M), 1.50 (L), 2.50 (XL) |
| linkup-wait-research | GET /research/{id}, polled 2 s doubling to 10 s, time-boxed, resumable | free |
| linkup-get-research | GET /research/{id} | free |
| linkup-list-research | GET /research | free |
| linkup-tasks-create | POST /tasks, 1 to 100 items, `dry_run` returns the cost estimate | as the wrapped calls |
| linkup-tasks-get | GET /tasks/{id} | free |
| linkup-tasks-list | GET /tasks, with type and status filters and the concurrency quota | free |
| linkup-credits-balance | GET /credits/balance | free |
| linkup-extract, linkup-get-extract, linkup-list-extract, linkup-extract-download | /extract (closed beta: 403 unless enabled) | variable, 2 to 10 typical |
| linkup-rewind-search, linkup-rewind-fetch | /rewind (closed beta) | not published |
| linkup-responses | POST /responses (OpenAI Responses format) | not published |
| linkup-guide | documentation, no network | free |

Resources: `linkup://guide` (the full guide) and `linkup://openapi.json` (the spec snapshot).

Behaviour worth knowing:

- Research tasks are created once and polled. `linkup-wait-research` returns after
  `max_wait_seconds` (default 600) with `done: false` if the task still runs and can be
  called again with the same id. It never re-creates a task.
- Every response is capped by `max_chars`. Truncation order: raw content and source snippets
  first, then trailing sources, the answer or markdown last. `_truncated` and `_hint` say
  when it happened. Research answers come back byte-identical to the API unless they alone
  exceed the cap.
- `linkup-list-research` and `linkup-tasks-list` return an `output_preview` per task;
  `include_output: true` returns the full outputs.
- Errors carry Linkup's status code, its error body, and a one-line hint (for example 429:
  rate limit or empty balance).

## Install

With [uv](https://docs.astral.sh/uv/), no checkout needed:

```
uvx --from git+https://github.com/cybernagl/linkup-mcp linkup-mcp
```

Claude Code, user scope (the key goes into the env of the entry, never on the command line):

```
claude mcp add-json --scope user linkup '{"type":"stdio","command":"uvx","args":["--from","git+https://github.com/cybernagl/linkup-mcp","linkup-mcp"],"env":{"LINKUP_API_KEY":"<key>"}}'
```

Any other MCP client: run `linkup-mcp` (or `python -m linkup_mcp`) over stdio with
`LINKUP_API_KEY` in the environment.

Environment: `LINKUP_API_KEY` (required), `LINKUP_BASE_URL` (default
`https://api.linkup.so/v1`), `LINKUP_TIMEOUT` (seconds per HTTP request, default 180).

From a checkout:

```
git clone https://github.com/cybernagl/linkup-mcp && cd linkup-mcp
uv venv && uv pip install -e .
.venv/bin/linkup-mcp
```

## Test

```
.venv/bin/python tests/test_wait.py       # offline: wait loop, error mapping, truncation, list summaries
.venv/bin/python tests/selftest.py        # protocol checks, plus free live calls when LINKUP_API_KEY is set
.venv/bin/python tests/selftest.py --paid # adds two fast searches and two fetches, about 0.014 USD
```

The self-test never creates a research task (0.25 USD and up).

## Claude Code Notes

- Results above 50,000 characters (or `MAX_MCP_OUTPUT_TOKENS`, default 25,000 tokens) are
  saved to a file by Claude Code and handed over as a path. The default `max_chars` caps
  (40,000 for search, fetch and lists; 48,000 for a research or task result) keep results
  inline; raise `max_chars` when you want everything.
- `MCP_TOOL_TIMEOUT` defaults to about 28 hours and stdio servers have no per-request
  timer, so long research waits are safe.

## Updating the Guide

Prices, limits and wording come from Linkup's documentation
(https://docs.linkup.so/llms.txt). They live in `src/linkup_mcp/server.py` next to
`DOCS_READ_DATE`. When Linkup changes something, update the text and the date, and replace
`src/linkup_mcp/openapi.json` with a fresh download of the spec.

## License

MIT.
