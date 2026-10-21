<!-- mcp-name: com.mireye/earth -->

# mireye-mcp

> Developed in a private monorepo. This repository
> ([Mireye-Labs/mireye-earth-mcp](https://github.com/Mireye-Labs/mireye-earth-mcp))
> is the published source of the `mireye-mcp` PyPI package, snapshot-synced
> on every release. Issues and PRs are welcome here (changes are applied upstream
> and re-synced; the test suite runs in the monorepo); releases land via PyPI.

Expose Mireye Earth's Search, Enrich, Fetch, question-answering, geocoding,
lookup, proximity, and field-request endpoints to MCP clients
that need a local stdio adapter (Claude Desktop, Cursor, custom agents
built on the `mcp` Python SDK) as native tools — no HTTP wiring required.

This is a standalone PyPI package (`mireye-mcp`) using `httpx` and `mcp`
(plus `tomli` on Python 3.10). It does not pull in the geospatial backend,
so there's no GDAL / rasterio / DuckDB build step.

The server is a local stdio adapter with no geospatial business logic:
tool handlers POST to the deployed Mireye HTTP API, while read-only MCP
resources fetch and cache the public field catalog.

## Guided agent setup (0.7.1)

Sign in at [Mireye agent setup](https://www.mireye.com/onboarding), copy the
setup prompt, and paste it into your coding agent. The prompt includes your
API key and setup identifiers automatically. You do not need to create or enter
a key manually. It runs:

```bash
uvx --from mireye-mcp==0.7.1 mireye-mcp connect --all --api-key '<api-key>' --attempt-id '<attempt-id>' --token-id '<token-id>'
uvx --from mireye-mcp==0.7.1 mireye-mcp doctor --proof
```

`connect` detects Claude Code, Codex, and Cursor, validates and saves the API key,
registers the stdio server, and installs the bundled `use-mireye` skill.
`doctor --proof` checks the actual installed entries, starts their MCP command,
initializes MCP, lists tools, and calls `mireye_fetch` for elevation at
40.7128, -74.0060. It reports completion only when the real result has an `ok`
status, a numeric value, and source provenance. This makes one small Fetch
request; the website completes when the server observes the first successful
call. Installation and verification are separate diagnostic milestones. Restart
your agent to load its new connection and skill.

The skill helps the agent choose Mireye for relevant US location and property
tasks in future conversations, pick the right tool, and retain citations,
location uncertainty, and failed fields. Setup writes these user-scoped files:

| Client / purpose | MCP configuration | Skill / credential file |
| --- | --- | --- |
| Claude Code | `~/.claude.json` | `~/.claude/skills/use-mireye/SKILL.md` |
| Codex | `~/.codex/config.toml` | `~/.agents/skills/use-mireye/SKILL.md` |
| Cursor | `~/.cursor/mcp.json` | `~/.agents/skills/use-mireye/SKILL.md` (shared with Codex) |
| Local login | — | `~/.config/mireye-mcp/credentials.json` (mode `0600`) |
| Setup ownership | — | `~/.config/mireye-mcp/onboarding.json` |
| Setup writer lock | — | `~/.config/mireye-mcp/onboarding.lock` (retained for coordination) |

MCP entries contain the credential file path, not the token. Setup does not
modify `AGENTS.md`, `CLAUDE.md`, project files, or shell profiles. It preserves
unrelated settings, backs up existing changed MCP files beside the original,
and refuses conflicting entries or customized skills. Repeating `connect`
reuses the stored login and unchanged files. The website also reuses a valid
setup key on repeated copies. Normal API-key expiry and revocation apply;
there is no short-lived setup code or cleanup policy.

Installation and removal share a local writer lock and recheck observed file
contents before mutation, preserving intervening edits. Retry after another
setup finishes. Codex inline `mcp_servers = { ... }` tables require manual
configuration or conversion to standard `[mcp_servers.name]` tables; preflight
reports this before authentication. Dry runs do not create the lock file.

Useful options, placed after `connect` or `doctor`:

```bash
# Preview all proposed paths without authenticating or writing anything.
uvx --from mireye-mcp==0.7.1 mireye-mcp connect --all --dry-run
# Target one installed client; --client can be repeated.
uvx --from mireye-mcp==0.7.1 mireye-mcp connect --client codex
# Read a credential-redacted report; --proof is optional.
uvx --from mireye-mcp==0.7.1 mireye-mcp doctor --proof --json
# Remove unchanged owned entries and skills; keep the login for reuse.
uvx --from mireye-mcp==0.7.1 mireye-mcp connect --all --remove
```

`--all` skips absent supported clients and reports them; an explicitly selected
missing or unsupported client is an error. Custom `CODEX_HOME` or
`CLAUDE_CONFIG_DIR` values are currently rejected by automatic setup; configure
those clients manually. `--config-home PATH` (or `MIREYE_MCP_SETUP_HOME`) isolates
all setup files under a test home without changing `HOME` or `CODEX_HOME`.
For development, `connect --package-source /absolute/path/to/mcp_server` installs
the local package through the current Python environment; it must already have
the MCP dependencies. Both commands accept `--base-url`; a directly supplied
API key is accepted only for `https://api.mireye.com` or loopback development servers.

The guided path requires the 0.7.1 package and the onboarding API deployment.
Existing manual local device login and hosted MCP OAuth remain available below.
Mireye's hosted remote MCP endpoint is `https://api.mireye.com/mcp` for clients
that support Streamable HTTP and native OAuth.

---

## What the agent gets

Nine tools, all prefixed `mireye_` so they sort together and don't
collide with generic `ask` / `fetch` tools from other MCP servers:

| Tool             | When the agent should call it                                                                  |
|------------------|------------------------------------------------------------------------------------------------|
| `mireye_ask`     | The caller asked a question about a US place ("is this in a flood zone?", "wildfire risk?").   |
| `mireye_proximity` | The caller wants drive-time distance, nearest-candidate ranking, proximity screening, or labor-shed stats across multiple coordinates. |
| `mireye_fetch`   | The caller wants specific named fields ("elevation and slope here") or is powering a workflow. |
| `mireye_geocode` | The caller gave an address and just wants the coordinate + its quality, nothing else. |
| `mireye_lookup` | The input might be ambiguous, isn't a clean address (a coordinate or an APN), or a parcel is wanted. |
| `mireye_request_field` | The catalog doesn't have the field the caller needs — describe it in plain language plus example locations and either get a live match now or a `request_id` to build it. |
| `mireye_field_request_status` | Poll a `request_id` from `mireye_request_field` for status, queue position, ETA, and (once `live`) the `resume` call. |
| `mireye_search` | Find the first parcels satisfying every supplied parcel and Fetch filter, with explicit coverage limits. |
| `mireye_enrich` | Resolve one US parcel and return selected Enrich attributes and calculations with field-level provenance. |

Catalog context is exposed as MCP resources instead of extra tools:

| Resource | What it returns |
|----------|-----------------|
| `mireye://catalog/fields` | Full field catalog. |
| `mireye://catalog/presets` | Preset names and field expansions. |
| `mireye://catalog/us-envelope` | Supported coordinate bounds. |
| `mireye://field/{name}` | One field definition. |
| `mireye://preset/{name}` | One preset expansion. |

Workflow prompts are also registered — 22 in total. Claude Code surfaces
MCP prompts as slash commands under the form `/mcp__<server>__<prompt>`.

| Prompt | What it does |
|--------|--------------|
| `mireye_ask` | Call `mireye_ask` with a lat/lng and question. |
| `mireye_fetch` | Call `mireye_fetch` with a lat/lng and optional fields/preset. |
| `mireye_lookup` | Call `mireye_lookup` with an address/`lat,lng`/APN, noting the `disposition` handling. |
| `mireye_request_field` | File a field request with `mireye_request_field`, noting the `disposition` handling. |
| `mireye_field_request_status` | Poll a field request's status with `mireye_field_request_status`. |
| `mireye_fields` | Browse or search the field catalog. |
| `mireye_pick_fields` | Choose the smallest useful field set for a free-text question. |
| `mireye_site_report` | Site report via the `site_selection` preset. |
| `mireye_flood_check` | Flood-relevant signals via the `flood_risk` preset. |
| `mireye_wildfire_underwrite` | Wildfire underwriting signals via the `wildfire_underwrite` preset. |
| `mireye_terrain_report` | Terrain signals via the `terrain` preset. |
| `mireye_land_cover_report` | Land cover signals via the `land_cover` preset. |
| `mireye_building_lookup_report` | Primary building details via the `building_lookup` preset. |
| `mireye_points_of_interest_report` | Nearby amenities via the `points_of_interest` preset. |
| `mireye_utilities_report` | Utility infrastructure via the `utilities` preset. |
| `mireye_boundaries_report` | Political/census boundaries via the `boundaries` preset. |
| `mireye_solar_siting_report` | Solar siting signals via the `solar_siting` preset. |
| `mireye_wind_siting_report` | Wind siting signals via the `wind_siting` preset. |
| `mireye_storage_siting_report` | Battery storage siting signals via the `storage_siting` preset. |
| `mireye_data_center_siting_report` | Data center siting signals via the `data_center_siting` preset. |
| `mireye_grid_interconnect_report` | Interconnection signals via the `grid_interconnect` preset. |
| `mireye_natural_hazard_report` | Natural hazard signals via the `natural_hazard` preset. |

There is no separate `list_fields` tool. Agents that need the Fetch catalog should
read the MCP resources above; the stdio adapter backs them with
`GET /v1/meta/fields` and a 1-hour ETag-aware cache. Search and Enrich selectors
come from their separate [Search field reference](https://docs.mireye.ai/api-reference/search-fields)
and [Enrich field reference](https://docs.mireye.ai/api-reference/enrich-fields).

---

## Install local stdio — one command with `uvx`

```bash
uvx --from 'mireye-mcp>=0.6.1' mireye-mcp
```

That's it. `uvx` (bundled with [`uv`](https://docs.astral.sh/uv/))
downloads the package into a managed cache, runs the entry point, and
the next invocation is instant. No venv to manage, no `pip install`,
no native builds.

The required-state Search contract and revised Enrich field metadata require
`mireye-mcp` **0.6.1 or later**; guided setup requires **0.7.1**. Refresh an older
cached install with `uvx mireye-mcp@latest`, then restart your MCP client so
it reloads the tool schemas. The version requirement in the configurations below
prevents an older cached adapter from satisfying the request.

If you don't have `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Plain pip also works:

```bash
pip install --upgrade 'mireye-mcp>=0.6.1'
mireye-mcp  # entry point
```

---

## Official MCP Registry

This server is published to the
[Official MCP Registry](https://registry.modelcontextprotocol.io) as
**`com.mireye/earth`** — the entry is live (the publish job ships from
`release.yml`). The registry entry carries both distributions:

- the **PyPI package** `mireye-mcp` (local stdio, run via `uvx`), and
- the **hosted remote** `https://api.mireye.com/mcp` (Streamable HTTP +
  OAuth) for clients that prefer a remote server.

Because the entry is live, registry-aware clients (VS Code, the GitHub MCP
Registry, and anything else that reads the official registry) can
discover and install it from there — no manual config required.

---

## Wire it into Claude Desktop

First authenticate the local adapter:

```bash
uvx --from 'mireye-mcp>=0.6.1' mireye-mcp login
```

For non-interactive hosts, set `MIREYE_BEARER_TOKEN` instead.

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "mireye-earth": {
      "command": "uvx",
      "args": ["--from", "mireye-mcp>=0.6.1", "mireye-mcp"]
    }
  }
}
```

Restart Claude Desktop. The nine tools (`mireye_ask`, `mireye_proximity`, `mireye_search`,
`mireye_enrich`, `mireye_fetch`, `mireye_geocode`, `mireye_lookup`,
`mireye_request_field`, `mireye_field_request_status`)
appear under the 🔌 menu, with catalog resources and prompts available to
clients that surface those MCP primitives.

To point at a self-hosted deployment instead of the default Fly URL:

```json
{
  "mcpServers": {
    "mireye-earth": {
      "command": "uvx",
      "args": ["--from", "mireye-mcp>=0.6.1", "mireye-mcp"],
      "env": {
        "MIREYE_BASE_URL": "https://your-deploy.example.com"
      }
    }
  }
}
```

---

## Wire it into Claude Code

For a manual hosted connection using Claude Code's native browser OAuth flow:

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
      "args": ["--from", "mireye-mcp>=0.6.1", "mireye-mcp"]
    }
  }
}
```

Open **Cursor → Settings → MCP** and confirm `mireye-earth` shows nine
tools with green status indicators. Catalog resources and prompts appear when the client supports
those MCP primitives.

---

## Wire it into a custom agent

If you're building an MCP client with the `mcp` Python SDK, point its
`StdioServerParameters` at the installed entry point:

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(
    command="uvx", args=["--from", "mireye-mcp>=0.6.1", "mireye-mcp"]
)

async with stdio_client(params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        # Inspect tool.name for the nine registered tools, including Search and Enrich.
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
| `MIREYE_TIMEOUT_S` | `120`                            | Per-request timeout in seconds (must exceed the ~110 s `/v1/ask` deadline). Search uses at least 210 seconds; a larger configured timeout is honored. |
| `MIREYE_BEARER_TOKEN` | unset | Optional bearer token for manual launches; overrides stored credentials. Guided connections set it empty so the server and `doctor` use the saved setup key. `status` reports on the stored login first. |
| `MIREYE_MCP_CREDENTIALS_FILE` | `~/.config/mireye-mcp/credentials.json` | Stored token path used by `login` / `status` / `logout` and guided setup. |
| `MIREYE_MCP_SETUP_HOME` | unset | Isolated home layout for `connect` / `doctor`; `--config-home` takes precedence. |

## Authentication

The local stdio adapter does not perform native MCP OAuth discovery.
The HTTP API requires bearer auth for tool calls. Guided `connect` already
stores that login. For manual setup, run the device login helper once:

```bash
uvx --from 'mireye-mcp>=0.6.1' mireye-mcp login
```

The command prints a verification URL and code, waits for approval in
the Mireye account page, and stores a Mireye API token locally. You can
also provide a token directly:

```json
{
  "mcpServers": {
    "mireye-earth": {
      "command": "uvx",
      "args": ["--from", "mireye-mcp>=0.6.1", "mireye-mcp"],
      "env": {
        "MIREYE_BEARER_TOKEN": "eyJ..."
      }
    }
  }
}
```

Check or remove local credentials:

```bash
uvx --from 'mireye-mcp>=0.6.1' mireye-mcp status
uvx --from 'mireye-mcp>=0.6.1' mireye-mcp logout
uvx --from 'mireye-mcp>=0.6.1' mireye-mcp logout --revoke
```

### Credentials are bound to their base URL

`login` records the `MIREYE_BASE_URL` it ran against, and the stored
token is only ever sent to that same URL. If `MIREYE_BASE_URL` later
points somewhere else, tool calls behave as logged out and the error
names both URLs — re-run `uvx --from 'mireye-mcp>=0.6.1' mireye-mcp login` against the new URL,
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
fields depend on slow upstream federal services. The default
`MIREYE_TIMEOUT_S` (120 s) already exceeds the ~110 s `/v1/ask` deadline, so a
cold start fits; only raise it further (never below 120) if you still time out.

**HTTP 400 `coord_out_of_bounds`.** Mireye is US-only in V1. The accepted
envelope is `lat ∈ [18, 72]`, `lng ∈ [-180, -65]` — covering the lower 48,
Alaska, Hawaii, and US territories.

**HTTP 400 `fields_unknown`.** Use the catalog for the tool being called.
Fetch names are listed at `https://api.mireye.com/v1/meta/fields`; Enrich names
are in the [Enrich field reference](https://docs.mireye.ai/api-reference/enrich-fields).
Fetch examples: `elevation_m` is `elevation`, `floodplain` is
`within_floodplain_polygon`.

**HTTP 4xx/5xx in general.** Tool errors include actionable JSON fields
such as `code`, `message`, `http_status`, `request_id`, `tool`, and
`retryable`. Agents should retry only when `retryable` is true.
Billing errors preserve `retry_after_seconds` when supplied and the raw
`retry_after` header, which can be seconds or an HTTP-date credit reset time.
`billing_degraded: true` means the billing service used fallback counters or
an estimated period. These fields appear at the error root on both transports.

**Proximity request limits.** `distance` and `screen` accept at most 10,000
origin-by-destination or origin-by-anchor pairs and 25 address-form locators
across both lists. The adapter applies the same validation as the hosted API
before sending a request. Split larger batches or supply coordinates for
addresses you already know.

**Auth errors.** `401` means the MCP server has no token, the token
expired, or the token was revoked. Run `uvx --from 'mireye-mcp>=0.6.1' mireye-mcp login` again,
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

The private monorepo's `release.yml` owns the complete release: tests, PyPI,
Official MCP Registry, then a snapshot of this directory to the public mirror.
It runs on `v*` tags in the private monorepo, with a guard requiring the tag to match
the root `pyproject.toml` version.

The coordinated **0.7.1** release aligns the root package and lockfile with
`mcp_server/pyproject.toml`, `mcp_server/uv.lock`, and both version entries in
`mcp_server/server.json`. It adds `connect`, `doctor`, and the bundled
`use-mireye` skill with direct API-key onboarding. Deploy the onboarding API,
publish and verify the package, then deploy the website that generates its
copyable setup command. After the release changes are approved and the real
onboarding flow is verified locally, tag that approved monorepo commit `v0.7.1` and push
the tag to start the existing release process. The public mirror receives its
package tag from the workflow; tagging the mirror alone does not publish.

Follow the run through PyPI, registry publication, and mirror sync; re-run a
failed job rather than moving a release tag. Verify that the published wheel
contains `mireye_earth_mcp/skills/use-mireye/SKILL.md`, that `connect --dry-run`
works, and that isolated API-key setup followed by `doctor --proof` launches
the installed MCP and returns cited elevation. Retain the existing Search and
Enrich schema checks. Verify that the public mirror's `v0.7.1` contains the same
package source. A local build or merged PR alone does not publish.

From this package directory, check distributions locally without publishing:

```bash
python -m build
```

### Parcel Search V1

`POST /v1/search` (MCP: `mireye_search`) accepts one `filters` list of hard
conditions, optional output-only `fields`, `limit`, and a continuation `cursor`.
Include a `state` or `state2` filter with `eq` for one two-letter US state code or
`in` for a nonempty list (50 states and DC); `ne` alone is insufficient. County and
acreage are optional filters. It returns the first matches, not ranked top matches.
Only the Search filter catalog defines valid filters; selecting output extras does
not expand it. Defaults: 10 results per page and a configurable 180-second budget.
Production uses the same deadline with candidate/evaluation caps of 1,000/500.
Set `limit` from 1 to 100 on each request, including cursor requests. The response always
includes `next_cursor`; use a non-null cursor with the same filters, fields and enrich
value to continue. Cursors expire after one hour. Over-limit requests are rejected.

The two output modes retain top-level parcel identity:

- **`enrich: false` (default):** a plain `fields` map contains filter values and
  requested extras, excluding duplicates of top-level identity. Extras may be any
  public Search or Enrich field, including public geometry and identifiers. They
  do not affect matching and are included in normal Search pricing. Missing or
  failed extras are `null`; failed work makes the overall response partial.
- **`enrich: true`:** the service attempts the full current Enrich catalog,
  regardless of the request's `fields` list. `fields` does not select an enrichment
  subset. Each match has a direct `enriched_fields` map of compact cells, with no
  match-level `fields` or nested Enrich envelope. Its field order, cell metadata,
  sparse omissions and temporary 33-field null allowlist are the same as standalone
  Enrich below. An empty `enriched_fields: {}` still means enrichment was requested.

Fresh matches omit `field_status`, `enrichment`, `enrichment_status`, and
`enrichment_error`. Check response-level `status` and `completion_reason`, and read
the single top-level billing receipt. Compact nulls and omissions cannot identify
per-field failures or establish absence. Failed enrichment retains a match and
makes Search partial; partial results cannot establish no-match.

Point work uses the matched Regrid latitude/longitude unchanged, reported with
`analysis_point.method: "regrid_lat_lon"`; it is not a computed interior point.
A missing or invalid coordinate pair skips that candidate, continues searching,
and makes the response partial. Polygon calculations retain the original parcel
geometry. Point samples describe the supplied point, not the whole parcel.
Enrichment reuses the selected parcel without geocoding or a new parcel lookup.
Both modes share the 180-second budget. Use a client timeout of at least 210 seconds.

The MCP adapter passes through the response JSON unchanged. Saved legacy responses
can retain their previous shape, coordinate method, and detailed provenance;
preserve all metadata actually returned rather than rewriting it to the fresh
contract. Operational counts are internal. See
[the Search contract](https://docs.mireye.ai/api-reference/search).


### Parcel Enrich

`POST /v1/enrich` (MCP: `mireye_enrich`) resolves one US parcel from `address` or
`lat`+`lng` and returns selected attributes and calculations with source metadata.
Pass the same HTTP object inside `req` for MCP. Select individual Enrich `fields`,
one Enrich `preset`, or both; omitting both requests all fields. Start with a small
set. Check `parcel_match.status` and confirm the parcel identity before using fields.
Only `matched` returns fields; other match outcomes return evidence and empty fields.
Enrich selectors differ from Fetch selectors. Use the current
[Enrich field catalog](https://docs.mireye.ai/api-reference/enrich-fields).

The flat `fields` map follows Enrich preset order and each preset's field order.
Shared fields appear once at their first preset; unassigned fields follow in
catalog order. Each returned field contains only `value`, `source`, `confidence`,
and `interpretation_hints`, plus `unit` when nonnull. A missing unit is omitted.

Failed fields are always omitted. Null values are retained only for a temporary
fixed allowlist of 33 fields; these nulls may mean meaningful absence or missing
information. Read `interpretation_hints` and related values: a retained null alone
does not prove absence. Null-valued fields outside the allowlist are omitted.
An omitted field is unknown, not a zero or negative finding. Preserve unfamiliar
field IDs, nested JSON values, zero, false, empty strings and empty lists.

Field metadata `source_url`, `fetched_at`, `dataset_vintage`, `ttl_seconds`, `notes`,
`status`, `lifecycle`, `error`, and `retryable` stays internal, as do top-level
`fetched_at` and `partial_failures`. Inline Search uses these same sparse compact
cells directly in `enriched_fields`. Saved legacy Search responses can still carry
their original detailed metadata; preserve what is returned. See the
[Enrich contract](https://docs.mireye.ai/api-reference/enrich).
