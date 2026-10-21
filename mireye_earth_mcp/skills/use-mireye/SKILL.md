---
name: use-mireye
description: Use Mireye for cited physical-world intelligence about US locations and properties, including terrain, environmental hazards, land use, utilities, parcels, and site screening. Apply when a task needs location-specific records or calculations, or explicitly requests Mireye. Generic geography explanations and map styling do not need this skill.
---

# Use Mireye

Answer questions about real places using Mireye's returned records, location quality, and citations. Preserve the user's chosen data sources and task scope.

## Choose a connection

Prefer an already connected Mireye MCP server. Inspect its advertised tools and input schemas before calling: hosted and local releases can expose different operations. The hosted endpoint is `https://api.mireye.com/mcp`.

If a required operation is unavailable through MCP, use the documented HTTP API when an API bearer token is already configured. Production base URL: `https://api.mireye.com`. Read the relevant endpoint in the [documentation index](https://docs.mireye.ai/llms.txt); do not infer routes or schemas from tool names.

Authentication paths are distinct:

- Hosted MCP uses client-managed OAuth. Its access tokens are scoped to MCP and do not authenticate general `/v1/*` requests.
- HTTP requests use `Authorization: Bearer <API token>`. The quickstart names the environment variable `MIREYE_API_TOKEN`.
- The local `mireye-mcp` adapter uses `MIREYE_BEARER_TOKEN` or its device login credentials at `~/.config/mireye-mcp/credentials.json`. Its `login`, `status`, and `logout` commands manage that login; the stored credential is bound to its API base URL.

If authentication is missing, identify the needed connection and use the [documented sign-in path](https://docs.mireye.ai/authentication). Keep credentials out of skill files, project instructions, and displayed output. For a broken local connection, run `mireye-mcp doctor` (or `uvx --from mireye-mcp==0.7.1 mireye-mcp doctor`). `doctor --proof` makes one real, small elevation Fetch call to verify the installed MCP transport and source provenance. Reconnect with `mireye-mcp connect --client <client>` when configuration is missing. This skill does not change client configuration on its own.

## Route the request

Use the following tools only when the connected server advertises them; follow that server's schema.

| User need | Tool |
| --- | --- |
| A natural-language question about a location | `mireye_ask` |
| Named data fields or a known preset at a location | `mireye_fetch` |
| Resolve an ambiguous locator or obtain a parcel match | `mireye_lookup` |
| Resolve a street address to a coordinate and inspect match quality | `mireye_geocode` |
| Driving relationships, nearest candidates, or a labor shed | `mireye_proximity` |
| Find parcels satisfying requirements | `mireye_search` |
| Investigate one identified parcel | `mireye_enrich` |

Discover Fetch fields and presets from advertised `mireye://catalog/fields` and `mireye://catalog/presets` resources, or public `GET https://api.mireye.com/v1/meta/fields`. Reuse the catalog for up to an hour, honoring its ETag when supported. Select the smallest useful field set. Do not invent field names or freeze a catalog count into the workflow. Search and Enrich have separate selectors: consult their [Search](https://docs.mireye.ai/api-reference/search) and [Enrich](https://docs.mireye.ai/api-reference/enrich) references instead of reusing Fetch's catalog.

## Resolve the intended place

`ask` and `fetch` accept either `lat` + `lng` or `address`, never both. A clean address can be sent directly; an extra geocode call is unnecessary unless inspecting the match first helps the task. These endpoints cover US locations; other operations can have different coverage.

Use conversation context to establish locality. If an address still lacks enough locality to identify the place, obtain that detail before querying. A relative description such as "the parcel north of it" needs an identified parcel or boundary evidence; do not turn it into an arbitrary coordinate offset.

For a plausible locator that may have multiple matches, use an available lookup operation and inspect `disposition`. Present candidates for `clarify`; do not silently choose one. On success, compare the returned location/address to the requested place. Preserve geocoding uncertainty: `parcel_grade: false` means the estimated point can describe a neighboring property. When lookup returns a matched parcel and an estimated geocode, use its `parcel.interior_point_lat` and `parcel.interior_point_lng` for subsequent point queries. Do not manufacture coordinates, parcel identifiers, or missing location details.

## Call and report

For direct HTTP `POST /v1/ask`, a minimal documented body is:

```json
{"lat": 40.7128, "lng": -74.0060, "question": "What is the elevation here?"}
```

Send JSON with the API bearer header. Allow at least 120 seconds for Ask; its server deadline is approximately 110 seconds. For progressive output, the documented `POST /v1/ask/stream` uses the same body. [Ask contract](https://docs.mireye.ai/api-reference/ask)

Inspect the response before summarizing and preserve the metadata each tool returns. Fetch retains its documented source links, freshness, material partial failures, and `ok`, `absent`, and `failed` statuses. Treat absent or unavailable data as unknown, not as evidence of zero risk; identify failed requested fields when returned.

Standalone `mireye_enrich` returns a sparse `fields` map. Search with `enrich: true` uses the same compact cells directly in `enriched_fields`. Each cell contains only `value`, `source`, `confidence`, and `interpretation_hints`, plus `unit` when nonnull. Failed fields are always omitted. Null values are retained only for a temporary fixed allowlist of 33 fields and may mean meaningful absence or missing information. Read `interpretation_hints` and related values: a retained null alone does not prove absence. Other null-valued fields are omitted. An omitted field is unknown, not zero or a negative finding. Preserve nested JSON, zero, false, empty values and returned nulls. Field source URLs, timestamps, vintage, TTL, notes, status, errors and retryability stay internal. Standalone Enrich also omits top-level `fetched_at` and `partial_failures`.

For `mireye_search`, filters remain limited to the Search filter catalog. With `enrich: false`, matches contain top-level identity plus a plain `fields` map of filter values and requested extras, excluding identity duplicates. Extras may be any public Search or Enrich field and are included in normal Search pricing; they never affect matching. Missing or failed extras are `null`; failed work makes the overall response partial. With `enrich: true`, the full current Enrich catalog is attempted regardless of request `fields`: that selector does not choose an enrichment subset. Matches have `enriched_fields` with no separate `fields` map or nested Enrich envelope. An empty `enriched_fields: {}` still means enrichment was requested.

Fresh Search matches omit `field_status`, `enrichment`, `enrichment_status`, and `enrichment_error`. Read response-level `status`, `completion_reason`, and the single top-level billing receipt. Do not infer per-field failure counts from compact nulls or omissions; partial results cannot establish no-match. Search point work preserves the matched Regrid latitude/longitude unchanged, with `analysis_point.method: "regrid_lat_lon"`. Do not substitute an interior or representative point. Candidates with missing or invalid pairs are skipped while search continues, making the response partial; parcel calculations keep the original geometry. Saved legacy responses can retain older shapes, coordinate methods, and detailed provenance. Preserve all returned metadata and do not rewrite a saved response to resemble a fresh one.

Distinguish measurements at a point from findings that cover a whole parcel. Never describe a point measurement as a parcel-wide finding without parcel coverage evidence. Retry only tool errors marked retryable, honoring `Retry-After`; refresh the catalog for unknown fields instead of guessing replacements.

If the task includes requesting missing coverage, use an advertised `mireye_request_field` with an `idempotency_key`. Check its disposition, retain any returned `request_id` in appropriate durable task state, and use the documented status/resume flow. Filing a request creates persistent work; do not do it merely because a lookup lacks data. [Field-request contract](https://docs.mireye.ai/api-reference/field-requests)
