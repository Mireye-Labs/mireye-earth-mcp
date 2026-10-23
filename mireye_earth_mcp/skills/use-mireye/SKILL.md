---
name: use-mireye
description: Use Mireye for cited physical-world intelligence about US locations and properties, including terrain, environmental hazards, land use, utilities, parcels, and site screening. Apply when a task needs location-specific records or calculations, or explicitly requests Mireye. Generic geography explanations and map styling do not need this skill.
---

# Use Mireye

Answer questions about real places using Mireye's returned records, location quality, and citations. Preserve the user's chosen data sources and task scope.

## Choose a connection

Prefer an already connected Mireye MCP server. Inspect its advertised tools and input schemas before calling: only the hosted service serves MCP tools. The hosted endpoint is `https://api.mireye.com/mcp`.

If a required operation is unavailable through MCP, use the documented HTTP API when an API bearer token is already configured. Production base URL: `https://api.mireye.com`. Read the relevant endpoint in the [documentation index](https://docs.mireye.ai/llms.txt); do not infer routes or schemas from tool names.

Authentication paths are distinct:

- Hosted MCP uses client-managed OAuth. Its access tokens are scoped to MCP and do not authenticate general `/v1/*` requests.
- HTTP requests use `Authorization: Bearer <API token>`. The quickstart names the environment variable `MIREYE_API_TOKEN`.
- The `mireye-mcp` package is an installation/diagnostic helper only. Its saved scoped API key at `~/.config/mireye-mcp/credentials.json` is private and bound to the API origin and account. It does not run a local MCP server or create an OAuth grant.

If authentication is missing, identify the needed connection and use the [documented sign-in path](https://docs.mireye.ai/authentication). Keep credentials out of skill files, project instructions, and displayed output. For setup diagnostics, run `uvx --from mireye-mcp==0.7.3 mireye-mcp doctor`. `doctor --proof` makes one free REST elevation Fetch and validates its provenance. This does not verify or authorize hosted MCP OAuth; complete the client’s sign-in separately. Reconnect with `mireye-mcp connect --client <client>` when configuration is missing. This skill does not change client configuration on its own.

## Route the request

Use the following tools only when the connected server advertises them; follow that server's schema.

| User need | Tool |
| --- | --- |
| A natural-language question about a location | `mireye_ask` |
| Named data fields or a known preset at a location | `mireye_fetch` |
| Resolve a street address to a coordinate and inspect match quality | `mireye_geocode` |
| Driving relationships, nearest candidates, or a labor shed | `mireye_proximity` |
| Find parcels satisfying requirements | `mireye_search` |
| Investigate one identified parcel | `mireye_enrich` |

Discover Fetch fields and presets from advertised `mireye://catalog/fields` and `mireye://catalog/presets` resources, or public `GET https://api.mireye.com/v1/meta/fields`. Reuse the catalog for up to an hour, honoring its ETag when supported. Select the smallest useful field set. Do not invent field names or freeze a catalog count into the workflow. Search and Enrich have separate selectors: consult their [Search](https://docs.mireye.ai/api-reference/search) and [Enrich](https://docs.mireye.ai/api-reference/enrich) references instead of reusing Fetch's catalog.

For Search, include a positive `state2` filter. Each match returns scalar filter
values and every name in `fields`, excluding values already represented by top-level
identity or `analysis_point`. `fields` accepts any public Search or Enrich field,
all included in the Search query price; read values directly, such as
`fields.landval`. Any other name returns `422 search_invalid_filter`, "Unknown Search
field: <name>." Keep paging while `next_cursor` is non-null: a short or empty page can
still carry a cursor, and null means the last page. Read `remarks` on every page and
branch on `code` and `path` only: `time_limit_reached`, `source_unavailable` and
`candidates_not_checked` mean matches may be missing; `field_unavailable` names a
requested field (`path` such as `fields[2]`) that is null in some matches. A null
does not prove absence.

For Enrich, `preset` is a list of one or more bundle names, such as `["power", "water"]`.

Search reuses the matched record's stored `lat`/`lon` unchanged with method
`parcel_stored_lat_lon`, including valid points outside the polygon. A candidate with
an invalid pair is not checked and is counted in a `candidates_not_checked` remark. Address filters query stored addresses
without geocoding. `parcel_match_type`, `parcel_match_distance_m` and
`parcel_match_radius_m` describe point lookups, so Search returns null when these
are requested.
Do not infer an exact intersection or zero lookup distance for filter-selected
parcels. Standalone Enrich retains its own locator and coordinate policy.

## Resolve the intended place

`ask` and `fetch` accept either `lat` + `lng` or `address`, never both. A clean address can be sent directly; an extra geocode call is unnecessary unless inspecting the match first helps the task. These endpoints cover US locations; other operations can have different coverage.

Use conversation context to establish locality. If an address still lacks enough locality to identify the place, obtain that detail before querying. A relative description such as "the parcel north of it" needs an identified parcel or boundary evidence; do not turn it into an arbitrary coordinate offset.

Use Geocode to inspect address-match quality or paid Enrich to resolve a parcel. Enrich selects a parcel from an address only when the geocode is rooftop-precision; otherwise it returns no_match with a reason and no charge, so retry with a fuller address or lat/lng. Lookup and Resolve are retired. Preserve geocoding uncertainty: an estimated coordinate can describe a neighboring property. Present ambiguous matches instead of silently choosing one. Do not manufacture coordinates, parcel identifiers, or missing location details.

Fetch and on-demand indexing are free. Fetch excludes parcel-dependent fields; use paid Enrich for parcel attributes. Ask costs $0.10 per answer. Search and Enrich are paid at the active plan's published dollar rates. Before paid work inspect the quoted price and balance, set a spending ceiling, and reuse the same operation ID for retries; do not start a new paid operation to recover a lost response. Do not refer to legacy credits.

## Call and report

For direct HTTP `POST /v1/ask`, a minimal documented body is:

```json
{"lat": 40.7128, "lng": -74.0060, "question": "What is the elevation here?"}
```

Send JSON with the API bearer header. Allow at least 120 seconds for Ask; its server deadline is approximately 110 seconds. For progressive output, the documented `POST /v1/ask/stream` uses the same body. [Ask contract](https://docs.mireye.ai/api-reference/ask)

Inspect the response before summarizing. Retain source links, units, confidence, freshness, location caveats, and material partial failures. Distinguish measurements at a point from findings that cover a whole parcel. Preserve `ok`, `absent`, and `failed` statuses when the endpoint returns them. Compact Enrich omits those statuses and failure details; do not invent them from missing keys. Treat null or unavailable data as unknown, not as evidence of zero risk, and report Search remarks that can leave matches unseen. Never describe a point measurement as a parcel-wide finding without parcel coverage evidence. Retry only errors marked retryable, honoring `Retry-After`; refresh the catalog for unknown fields instead of guessing replacements.

If the task includes requesting missing coverage, use an advertised `mireye_request_field` with an `idempotency_key`. Check its disposition, retain any returned `request_id` in appropriate durable task state, and use the documented status/resume flow. Filing a request creates persistent work; do not do it merely because a lookup lacks data. [Field-request contract](https://docs.mireye.ai/api-reference/field-requests)
