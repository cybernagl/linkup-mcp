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
  called again with the same id. It never re-creates a task. A network error, a timeout, an
  HTTP 429 or an HTTP 5xx during the wait does not end the wait. The tool polls again until
  `max_wait_seconds` and reports `transient_errors` and `last_error`.
- Every tool that returns results, tasks, lists or rows takes `max_chars` and keeps its
  response within it. The response is compact JSON. The cap applies to the text that is sent.
  The order of cuts is: raw content and source snippets, then other long strings, then
  trailing sources, results and images, then the answer or markdown, then trailing items of
  other lists. A `<list>_omitted` counter gives the number of dropped items. `_truncated`
  and `_hint` say when a cut happened. A research answer comes back byte-identical to the
  API when the answer and the task envelope (a few hundred characters) fit in the cap. The
  two create calls (`linkup-research`, `linkup-extract`), the balance and the guide have no
  cap, because their responses are small.
- Tool parameters are snake_case. A call with an unknown parameter name (for example
  `outputType`) fails with an error that names the valid parameters. No request is sent to
  Linkup.
- `linkup-list-research` and `linkup-tasks-list` return an `output_preview` per task;
  `include_output: true` returns the full outputs.
- Errors carry Linkup's status code, its error body, and a one-line hint (for example 429:
  rate limit or empty balance).

## Install

With [uv](https://docs.astral.sh/uv/), from [PyPI](https://pypi.org/project/linkup-mcp/),
no checkout needed:

```
uvx linkup-mcp
```

Claude Code, user scope. The key is stored in the `env` of the entry, not in its `args`.
`read -rs` keeps the key out of the shell history. The key is on the command line of the
`claude` process only while the command runs.

```
read -rs LINKUP_API_KEY   # paste the key, then press Enter; nothing is shown
claude mcp add-json --scope user linkup '{"type":"stdio","command":"uvx","args":["linkup-mcp"],"env":{"LINKUP_API_KEY":"'"$LINKUP_API_KEY"'"}}'
unset LINKUP_API_KEY
```

The current main branch instead of the release: replace `linkup-mcp` in `args` with
`--from`, `git+https://github.com/cybernagl/linkup-mcp`, `linkup-mcp`, or run

```
uvx --from git+https://github.com/cybernagl/linkup-mcp linkup-mcp
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
.venv/bin/python tests/test_wait.py       # offline: wait loop, error mapping, size cap, argument checks, list summaries
.venv/bin/python tests/selftest.py        # protocol checks, plus free live calls when LINKUP_API_KEY is set
.venv/bin/python tests/selftest.py --paid # adds two fast searches and two fetches, about 0.014 USD
```

The self-test never creates a research task (0.25 USD and up). GitHub Actions runs the
offline tests and the protocol self-test on Python 3.11, 3.12 and 3.13 for every push.

## Release

Releases reach PyPI through [trusted publishing](https://docs.pypi.org/trusted-publishers/):
GitHub Actions proves its identity to PyPI with an OIDC token, and PyPI mints a 15-minute
upload token for this project. No API token is stored anywhere.

1. Set the new version in `pyproject.toml` and commit it to main
2. Tag that commit and push the tag: `git tag -a vX.Y.Z -m "linkup-mcp X.Y.Z" && git push origin vX.Y.Z`
3. The Release workflow (`.github/workflows/release.yml`) checks that the tag matches the
   version, runs the tests, builds wheel and sdist, and then waits in the `pypi` environment
   for an approval on the Actions page. After the approval it uploads to PyPI

The publisher registered on PyPI is repository `cybernagl/linkup-mcp`, workflow
`release.yml`, environment `pypi`. The environment accepts `v*` tags only and requires a
reviewer. A run that fails before the upload can be re-run or re-tagged at the same version;
a version that reached PyPI cannot be replaced and needs a new version number.

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
