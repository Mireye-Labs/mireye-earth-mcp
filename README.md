<!-- mcp-name: com.mireye/earth -->

# mireye-mcp

> Developed in a private monorepo. This repository
> ([Mireye-Labs/mireye-earth-mcp](https://github.com/Mireye-Labs/mireye-earth-mcp))
> is the published source of the `mireye-mcp` PyPI package, snapshot-synced
> on every release. Issues and PRs are welcome here (changes are applied upstream
> and re-synced; the test suite runs in the monorepo); releases land via PyPI.

Expose Mireye Earth's `/v1/ask` and `/v1/fetch` endpoints to MCP clients
that need a local stdio adapter (Claude Desktop, Cursor, custom agents
built on the `mcp` Python SDK) as native tools — no HTTP wiring required.

This is a standalone PyPI package (`mireye-mcp`) with only two
runtime dependencies — `httpx` and `mcp`. It does not pull in the
geospatial backend, so there's no GDAL / rasterio / DuckDB build step.

The server is a local stdio adapter with no geospatial business logic:
tool handlers POST to the deployed Mireye HTTP API, while read-only MCP
resources fetch and cache the public field catalog.

Mireye also exposes a hosted remote MCP endpoint at
`https://api.mireye.com/mcp` for clients that support Streamable
HTTP and native OAuth. Use that hosted endpoint for Claude Code so `/mcp`
opens the browser sign-in flow. This package remains the local stdio path;
it uses `mireye-mcp login` or `MIREYE_BEARER_TOKEN` for credentials.

---

## What the agent gets

Two tools, both prefixed `mireye_` so they sort together and don't
collide with generic `ask` / `fetch` tools from other MCP servers:

| Tool            | When the agent should call it                                                                  |
|-----------------|------------------------------------------------------------------------------------------------|
| `mireye_ask`    | The caller asked a question about a US place ("is this in a flood zone?", "wildfire risk?").   |
| `mireye_fetch`  | The caller wants specific named fields ("elevation and slope here") or is powering a workflow. |

Catalog context is exposed as MCP resources instead of extra tools:

| Resource | What it returns |
|----------|-----------------|
| `mireye://catalog/fields` | Full field catalog. |
| `mireye://catalog/presets` | Preset names and field expansions. |
| `mireye://catalog/us-envelope` | Supported coordinate bounds. |
| `mireye://field/{name}` | One field definition. |
| `mireye://preset/{name}` | One preset expansion. |

Workflow prompts are also registered: `mireye_ask`, `mireye_fetch`,
`mireye_site_report`, `mireye_flood_check`, `mireye_wildfire_underwrite`,
and `mireye_pick_fields`. Claude Code surfaces MCP prompts as slash
commands under the form `/mcp__<server>__<prompt>`.

There is no third `list_fields` tool. Agents that need the catalog should
read the MCP resources above; the stdio adapter backs them with
`GET /v1/meta/fields` and a 1-hour ETag-aware cache.

---

## Install local stdio — one command with `uvx`

```bash
uvx mireye-mcp
```

That's it. `uvx` (bundled with [`uv`](https://docs.astral.sh/uv/))
downloads the package into a managed cache, runs the entry point, and
the next invocation is instant. No venv to manage, no `pip install`,
no native builds.

If you don't have `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Plain pip also works:

```bash
pip install mireye-mcp
mireye-mcp  # entry point
```

---

## Official MCP Registry

This server publishes to the
[Official MCP Registry](https://registry.modelcontextprotocol.io) as
**`com.mireye/earth`** (the entry goes live with the next tagged
release; the publish job ships from `release.yml`). The registry entry
carries both distributions:

- the **PyPI package** `mireye-mcp` (local stdio, run via `uvx`), and
- the **hosted remote** `https://api.mireye.com/mcp` (Streamable HTTP +
  OAuth) for clients that prefer a remote server.

Once the entry is live, registry-aware clients (VS Code, the GitHub MCP
Registry, and anything else that reads the official registry) can
discover and install it from there — no manual config required.

---

## Wire it into Claude Desktop

First authenticate the local adapter:

```bash
mireye-mcp login
```

For non-interactive hosts, set `MIREYE_BEARER_TOKEN` instead.

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "mireye-earth": {
      "command": "uvx",
      "args": ["mireye-mcp"]
    }
  }
}
```

Restart Claude Desktop. The two tools (`mireye_ask`, `mireye_fetch`)
appear under the 🔌 menu, with catalog resources and prompts available to
clients that surface those MCP primitives.

To point at a self-hosted deployment instead of the default Fly URL:

```json
{
  "mcpServers": {
    "mireye-earth": {
      "command": "uvx",
      "args": ["mireye-mcp"],
      "env": {
        "MIREYE_BASE_URL": "https://your-deploy.example.com"
      }
    }
  }
}
```

---

## Wire it into Claude Code

Use the hosted HTTP MCP endpoint instead of this local stdio package:

```bash
claude mcp remove mireye-earth -s user   # only needed if an old stdio entry exists
claude mcp add --transport http --scope user mireye-earth https://api.mireye.com/mcp
```

Restart Claude Code, run `/mcp`, and follow the browser OAuth flow.
Slash commands appear as:

- `/mcp__mireye-earth__mireye_ask <lat> <lng> <question>`
- `/mcp__mireye-earth__mireye_fetch <lat> <lng> [fields] [preset]`

Or just chat naturally — Claude Code will call the tool when relevant.

---

## Wire it into Cursor

Cursor's MCP config lives at `~/.cursor/mcp.json` (global) or
`<repo>/.cursor/mcp.json` (workspace). Same shape as Claude Desktop:

```json
{
  "mcpServers": {
    "mireye-earth": {
      "command": "uvx",
      "args": ["mireye-mcp"]
    }
  }
}
```

Open **Cursor → Settings → MCP** and confirm `mireye-earth` shows the two
green tools. Catalog resources and prompts appear when the client supports
those MCP primitives.

---

## Wire it into a custom agent

If you're building an MCP client with the `mcp` Python SDK, point its
`StdioServerParameters` at the installed entry point:

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(command="uvx", args=["mireye-mcp"])

async with stdio_client(params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        # tools.tools is [mireye_ask, mireye_fetch]
        resources = await session.list_resources()
        prompts = await session.list_prompts()

        result = await session.call_tool(
            "mireye_ask",
            {"lat": 40.7128, "lng": -74.0060, "question": "elevation?"},
        )
```

---

## Configuration

| Env var            | Default                          | Purpose                          |
|--------------------|----------------------------------|----------------------------------|
| `MIREYE_BASE_URL`  | `https://api.mireye.com`   | HTTP base URL the tools POST to. Stored login credentials only attach when they were created against this same URL. |
| `MIREYE_TIMEOUT_S` | `60`                             | Per-request timeout in seconds.  |
| `MIREYE_BEARER_TOKEN` | unset | Optional Mireye bearer token. Overrides stored credentials for tool calls; `status` reports on the stored login first. |
| `MIREYE_MCP_CREDENTIALS_FILE` | `~/.config/mireye-mcp/credentials.json` | Stored token path used by `login` / `status` / `logout`. |

## Authentication

The local stdio adapter does not perform native MCP OAuth discovery.
The HTTP API requires bearer auth for `/v1/ask` and `/v1/fetch`, so run
the device login helper once:

```bash
mireye-mcp login
```

The command prints a verification URL and code, waits for approval in
the Mireye account page, and stores a Mireye API token locally. You can
also provide a token directly:

```json
{
  "mcpServers": {
    "mireye-earth": {
      "command": "uvx",
      "args": ["mireye-mcp"],
      "env": {
        "MIREYE_BEARER_TOKEN": "eyJ..."
      }
    }
  }
}
```

Check or remove local credentials:

```bash
mireye-mcp status
mireye-mcp logout
mireye-mcp logout --revoke
```

### Credentials are bound to their base URL

`login` records the `MIREYE_BASE_URL` it ran against, and the stored
token is only ever sent to that same URL. If `MIREYE_BASE_URL` later
points somewhere else, tool calls behave as logged out and the error
names both URLs — re-run `mireye-mcp login` against the new URL,
or set `MIREYE_BEARER_TOKEN` explicitly. Credentials files without a
recorded `base_url` (e.g. hand-written) are treated as bound to the
default `https://api.mireye.com`.

Two more guardrails:

- Tokens of any kind are never sent over plain `http://`, except to
  loopback hosts (`localhost` / `127.0.0.1` / `[::1]`) for local
  development.
- `status` and `logout --revoke` operate on the **stored** base URL, so
  you can always inspect or revoke a stored login even while
  `MIREYE_BASE_URL` points elsewhere.

For native MCP OAuth, configure your client to use the hosted remote MCP
URL `https://api.mireye.com/mcp` instead of launching this stdio
binary. The remote endpoint advertises OAuth metadata and uses browser
OAuth 2.1 + PKCE.

---

## Troubleshooting

**Server doesn't appear in Claude Desktop.** Check that `uvx` resolves
on the PATH used by the GUI app (macOS launches GUI apps with a minimal
PATH). Test from a terminal: `which uvx`. If empty, install `uv`:
`curl -LsSf https://astral.sh/uv/install.sh | sh`. If `uvx` lives at
`/Users/you/.local/bin/uvx`, use the absolute path in `command`.

**Tools not appearing under the 🔌 menu after restart.** Watch
`~/Library/Logs/Claude/mcp-server-mireye-earth.log`. The server logs to
stderr on startup; you should see `[mireye-mcp] starting
base_url=…`. If there's no log, `uvx` isn't being invoked — usually a
PATH issue.

**`ConnectError` / `ReadTimeout`** on the first tool call. The hosted
API keeps its machines running, but calls right after a backend deploy
can be slow while geospatial sources warm in the background, and some
fields depend on slow upstream federal services. Increase
`MIREYE_TIMEOUT_S` to 90 if you are repeatedly hitting timeouts.

**HTTP 400 `coord_out_of_bounds`.** Mireye is US-only in V1. The accepted
envelope is `lat ∈ [18, 72]`, `lng ∈ [-180, -65]` — covering the lower 48,
Alaska, Hawaii, and US territories.

**HTTP 400 `fields_unknown`.** The field name is not in the catalog. Hit
`https://api.mireye.com/v1/meta/fields` to see the canonical list.
Common surprises: `elevation_m` is `elevation`, `floodplain` is
`within_floodplain_polygon`.

**HTTP 4xx/5xx in general.** Tool errors include actionable JSON fields
such as `code`, `message`, `http_status`, `request_id`, `tool`, and
`retryable`. Agents should retry only when `retryable` is true.

**Auth errors.** `401` means the MCP server has no token, the token
expired, or the token was revoked. Run `mireye-mcp login` again,
or set `MIREYE_BEARER_TOKEN`. `403` means the signed-in account is not
allowed by the backend account policy.

---

## What this server does NOT do

- **No data-result caching.** The HTTP API has its own response cache
  (local disk, plus a shared Redis tier in production). The stdio adapter
  only caches the public field catalog resource.
- **No geospatial business logic.** The adapter validates obvious MCP input
  bounds and field-count limits, but source orchestration stays in the API.
- **No streaming.** MCP streaming responses are V1.5.
- **No Firebase validation in MCP.** The MCP package forwards bearer
  tokens; the HTTP API owns token verification and account policy.
- **No in-process imports of `mireye_earth`.** The server talks to the API
  over HTTP only, so it ships as a separate slim PyPI package
  (`mireye-mcp`) and can be installed without the data backend.

---

## Releasing

This package is published from this directory on every `v*` tag on the
main repo. To release locally:

```bash
cd mcp_server
python -m build
twine upload dist/*
```
