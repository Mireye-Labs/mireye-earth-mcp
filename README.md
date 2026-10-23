# Mireye hosted MCP setup helper

`mireye-mcp` 0.7.3 configures Claude Code, Codex and Cursor to connect directly to
**https://api.mireye.com/mcp**, and installs the `use-mireye` skill. The package
runs no MCP server. Its default invocation refuses the retired local stdio
transport; device login and local data tools are not included.

Sign in at [Mireye agent setup](https://www.mireye.com/onboarding), accept the
current customer terms, copy the private setup prompt and paste it into your
agent. The prompt supplies the scoped API key through standard input, never a
process argument. The helper validates the account and exact setup-key identity
before saving the key or changing client configuration.

```bash
# The agent supplies the private key on stdin, without echoing or logging it.
uvx --from mireye-mcp==0.7.3 mireye-mcp connect --all --api-key-stdin --attempt-id '<attempt-id>' --token-id '<token-id>'
uvx --from mireye-mcp==0.7.3 mireye-mcp doctor --proof
```

`connect` writes native hosted configurations, without API keys or local launch
commands. Restart your agent and authorize its hosted MCP connection using the
client's OAuth sign-in. OAuth credentials remain managed by that client.

`doctor --proof` verifies the installed configuration and skill, checks the saved
API key's account, and makes **one free REST Fetch** for elevation at 40.7128,
-74.0060. It requires a finite value, `ok` status, source URL and timestamp before
reporting the data proof. The API records the successful Fetch against the setup
attempt. This proves the REST key/data path; **it does not authorize or verify
hosted MCP OAuth**. Confirm the connected client's tools after OAuth sign-in.

| Client / purpose | Configuration | Skill / credentials |
| --- | --- | --- |
| Claude Code | `~/.claude.json` | `~/.claude/skills/use-mireye/SKILL.md` |
| Codex | `~/.codex/config.toml` | `~/.agents/skills/use-mireye/SKILL.md` |
| Cursor | `~/.cursor/mcp.json` | `~/.agents/skills/use-mireye/SKILL.md` (shared with Codex) |
| Helper API key | — | `~/.config/mireye-mcp/credentials.json` (mode `0600`) |
| Setup ownership | — | `~/.config/mireye-mcp/onboarding.json` |

Setup preserves unrelated configuration, refuses custom conflicting entries or
modified skills, and backs up changed configuration files. A process lock and
content rechecks protect concurrent edits. Repeating `connect` reuses the saved
key and files; it does not issue credentials or grant another promotional
balance. An inherited shell key cannot replace an existing managed account.
To switch accounts, remove the managed connection, run `logout`, then connect
from that account's setup page. Authorize the correct account separately in the
MCP client's OAuth flow.

```bash
# Preview only: no key read, API requests, login save, lock or file changes.
uvx --from mireye-mcp==0.7.3 mireye-mcp connect --all --dry-run
# Reuse the saved key; --client can be repeated.
uvx --from mireye-mcp==0.7.3 mireye-mcp connect --client codex
# Read a redacted diagnostic report.
uvx --from mireye-mcp==0.7.3 mireye-mcp doctor --proof --json
# Remove unchanged owned entries/skills, retaining the helper API key.
uvx --from mireye-mcp==0.7.3 mireye-mcp connect --all --remove
# Remove the helper's saved key. --revoke also revokes its server-verified key ID.
uvx --from mireye-mcp==0.7.3 mireye-mcp logout
```

`--all` skips absent supported clients; explicitly selecting an absent client
fails. Existing identical manual entries remain unmanaged, including on removal.
No project instructions or shell profiles are changed. Custom `CODEX_HOME` and
`CLAUDE_CONFIG_DIR` locations require manual setup; use `--config-home PATH` for
isolated tests. `--base-url` accepts the production API or loopback only and
binds saved keys to that origin. Redirects never forward a key to another host.
Legacy `--api-key` input remains accepted for compatibility; prefer stdin so the
key cannot appear in process listings.

Native configurations follow the client documentation:
[Claude Code](https://code.claude.com/docs/en/mcp),
[Codex](https://developers.openai.com/codex/mcp), and
[Cursor](https://cursor.com/docs/mcp).

The official MCP registry entry is hosted-only. This package is not a local
transport distribution, and installing it does not add a second data service.

## Enrich results

The hosted `mireye_enrich` tool returns the same compact response as
`POST /v1/enrich`. Each returned field contains `value`, `source`, `confidence`,
and `interpretation_hints`, with `unit` only when defined. Failed fields are
omitted. A matched parcel can return `fields: {}`.

Coordinates, geocode evidence, the analysis point, parcel match status and
billing are included. `mireye_enrich` takes `preset` as a list of one or more
bundle names, such as `["power", "water"]`.

`mireye_search` returns scalar filter values and every requested public Search or
Enrich field as plain values in each match's `fields`, excluding top-level identity
duplicates, using the matched parcel record's `lat`/`lon` pair. Every response carries
`remarks` (`[]` when clean; branch on `code` and `path`) and `next_cursor` (null on the
last page). Fetch and Ask have their own contracts.
See the [Enrich reference](https://docs.mireye.ai/api-reference/enrich) for the
525-field inventory, selectors and examples.
