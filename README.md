# sn-community-events-mcp

MCP server that fetches ServiceNow Community events (SNUGs, webinars, World Forum
stops, etc. from https://www.servicenow.com/community/events/ct-p/TopLevel_Events)
for a date range and returns them as structured data, optionally also writing a CSV.

No login or credentials are required — the underlying API is public.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) installed locally (`brew install uv` on
macOS). `uvx` fetches and runs the package straight from this repo — no manual
cloning or dependency install needed on your end, for either client below. Both
re-fetch from `main` on next launch, so you stay on the latest version automatically.

### Claude Code

```bash
claude mcp add sn-community-events -- uvx --from git+https://github.com/jon-lind-sn/sn-community-events-mcp sn-community-events-mcp
```

This registers the server at the user level (available in every project). To scope it
to just the current project instead, add `--scope project` before the `--`, which
writes the entry to `.mcp.json` in the current directory instead of your global config.

Verify it's registered with `claude mcp list`, and check connectivity with
`claude mcp get sn-community-events`.

### Claude Desktop

1. Install `uv` and open the config file:

   **macOS**

   ```bash
   brew install uv
   open -e ~"/Library/Application Support/Claude/claude_desktop_config.json"
   ```

   **Windows**

   ```powershell
   winget install --id=astral-sh.uv -e
   notepad "$env:APPDATA\Claude\claude_desktop_config.json"
   ```

2. Add the `sn-community-events` entry below.

   - If the file already has an `mcpServers` key, add `sn-community-events` as a new
     entry inside it, alongside whatever's already there:

     ```json
     {
       "mcpServers": {
         "sn-community-events": {
           "command": "uvx",
           "args": [
             "--from",
             "git+https://github.com/jon-lind-sn/sn-community-events-mcp",
             "sn-community-events-mcp"
           ]
         }
       }
     }
     ```

   - If the file is empty paste in the whole block above as-is.  If there is no `mcpServers` key, copy it and its content at the top of the file after the first curly brace.

3. Restart Claude Desktop for it to pick up the new server.

## Tool: `get_community_events`

| Argument | Required | Description |
|---|---|---|
| `start_date` | yes | Inclusive start of the date range, `YYYY-MM-DD` |
| `end_date` | yes | Inclusive end of the date range, `YYYY-MM-DD` |
| `status_category` | no | Filter slug. Only `upcoming` is confirmed to work reliably. |
| `location_category` | no | e.g. `in-person`, `virtual`, `hybrid` |
| `product_category` | no | e.g. `app-engine` (slugified UI label) |
| `type_category` | no | e.g. `webinar`, `workshop`, `academy`, `office-hours` |
| `include_full_description` | no, default `true` | Fetch each event's own page for its full description instead of the listing's truncated blurb. One extra HTTP request per matched event — set `false` for wide date ranges. |
| `save_csv_path` | no | If given, also write results to a CSV at this local path. |

Returns `{"events": [...], "meta": {...}}`. `meta` includes counts worth surfacing to
the user (how many events matched, how many full descriptions were/weren't fetched,
etc.).

## Development notes

All fetch/parse logic is in `src/sn_community_events_mcp/core.py` — read its
docstrings before changing anything. The short version: the API's `page` query
param is silently ignored (pagination is a keyset cursor via `cursor-k`/`cursor-b`
instead), and its own result-count fields are unreliable. Both cost real debugging
time to work out originally.

To run locally for development:

```bash
uv sync
uv run sn-community-events-mcp
```
