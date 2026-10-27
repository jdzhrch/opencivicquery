# OpenCivicQuery

OpenCivicQuery is an MCP server that lets reporters ask questions of government open data in plain language, from Claude, ChatGPT, or any other MCP client. It reaches every portal on the Socrata platform: cities, counties, states, and federal agencies.

Status: early development. The interface may change.

## Why the answers can be checked

Every answer that contains a number comes with:

- the counting unit, so "170 eviction notices" never turns into "170 evictions";
- a completeness flag, so a truncated result is never reported as a total;
- the exact query and a public link that reruns it on the city's own portal;
- the source's last update time;
- an evidence snapshot that freezes the rows behind the figure.

These rules live in the server and travel with every response, so they hold in any client, with or without an extra skill or prompt.

## Source packs

Anyone can query any dataset on any Socrata portal. A **source pack** adds what the raw data does not say: what one row means, how to count it, and the traps. For example, the New York City pack records that restaurant inspection rows are violation citations, so counting rows overstates the number of restaurants.

Datasets without a pack still work. Their responses carry a warning that the row unit is unknown.

## Which portals it reaches

Any portal the Socrata catalog lists. Dataset counts below come from the catalog on 2026-10-27 and change often.

| Level | Examples (datasets) |
|---|---|
| City | New York (2,406), Chicago (924), Austin (710), San Francisco (668), Los Angeles (361), Dallas (342), Oakland (313), Cambridge MA (281), Baton Rouge (240), New Orleans (209), Kansas City MO (198), Seattle (147), Buffalo (139), Providence (122) |
| County | Cook IL (523), Montgomery MD (471), King WA (268), Santa Clara CA (131) |
| State | Maryland (1,546), Washington (1,070), New York (1,035), Texas (830), Connecticut (654), Colorado (639), Oregon (539), Pennsylvania (352) |
| Federal | CDC (1,084), Department of Transportation (459), HealthData.gov (147) |

Some large publishers use other platforms and are not reachable yet, including Boston, Washington DC, Philadelphia, Denver, Baltimore, San Jose, Los Angeles County, California, and Massachusetts.

Current packs: San Francisco (`packs/us-ca-san-francisco.yaml`) and New York City (`packs/us-ny-new-york-city.yaml`). To add a city, see [Contributing a source pack](#contributing-a-source-pack).

## Install

A public instance runs without login at `https://opencivicquery-1040721507408.us-west1.run.app/mcp`. The plugin connects to it and adds the **Ask Open Data** skill, which teaches the assistant how to turn a reporter's question into a countable one, compare periods, and hand back evidence an editor can check.

### Claude: the plugin

The plugin has two parts: the skill, and a connection to the server. Claude installs the skill with the plugin, but the connection has to exist as a connector before the plugin can use it.

1. **Add the connector.** Paste the URL above as a custom connector with no authentication. On Pro and Max, open Customize, Connectors. On Team and Enterprise, an Owner adds it in Organization settings, Connectors, and each member then connects it under Customize, Connectors.
2. **Install the plugin.**
   - **Claude desktop app (paid plans):** open Customize, Plugins, Add, Add marketplace, and enter `jdzhrch/opencivicquery`. Then install OpenCivicQuery.
   - **Claude Code:** run `/plugin marketplace add jdzhrch/opencivicquery`, then `/plugin install opencivicquery@opencivicquery`.
   - **Organization-wide sync:** Claude's organization sync accepts only private or internal repositories, so it cannot sync this public one directly. An organization that wants central distribution can keep a private copy of this repository and sync that. Sync runs when a pull request that raises the plugin version merges into the default branch.

### Claude: connection only

In Claude on the web or desktop, open Settings, Connectors, Add custom connector, and paste the URL above. The server's own rules still apply; the skill is not included.

### ChatGPT

- **Business or Enterprise workspace:** an admin creates a custom app (MCP connector) with the URL above and no authentication, and copies its app ID (`asdk_app_...`). Then build the plugin package and publish it to the workspace:

  ```bash
  uv run python scripts/build_chatgpt_plugin.py --app-id asdk_app_YOUR_APP_ID
  ```

  The package in `dist/` holds the same skill as the Claude plugin. For a new version, rebuild and publish again; members do not reinstall.
- **Individual accounts:** add the URL as a custom connector in developer mode. The skill is not included.

The hosted instance caps each direct caller, such as Claude Code or a script, at 200 calls a day per IP address, on top of a server-wide daily cap. Claude and ChatGPT connectors reach the server from the vendors' own addresses on behalf of many users, so their calls count only toward the server-wide cap. Audit logs record a salted hash of the address, never the address itself. For heavy use, run your own copy.

## Quickstart: run it on your computer

You need [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/jdzhrch/opencivicquery.git
cd opencivicquery
uv sync
```

### Claude Code

```bash
claude mcp add opencivicquery \
  -e OPENCIVICQUERY_CONFIG_PATH="$PWD/deployments/local.yaml" \
  -e OPENCIVICQUERY_TRANSPORT=stdio \
  -e OPENCIVICQUERY_SNAPSHOT_PATH="$HOME/.opencivicquery/snapshots" \
  -- uv --directory "$PWD" run opencivicquery-server
```

### Claude Desktop

Add this to `claude_desktop_config.json`, with your own paths:

```json
{
  "mcpServers": {
    "opencivicquery": {
      "command": "uv",
      "args": ["--directory", "/path/to/opencivicquery", "run", "opencivicquery-server"],
      "env": {
        "OPENCIVICQUERY_CONFIG_PATH": "/path/to/opencivicquery/deployments/local.yaml",
        "OPENCIVICQUERY_TRANSPORT": "stdio",
        "OPENCIVICQUERY_SNAPSHOT_PATH": "/path/to/snapshots"
      }
    }
  }
}
```

Then ask, for example: "How many residential evictions were executed in New York City in 2025, by borough?"

The local setup uses no login and no Socrata app token. Socrata serves token-less requests from a shared pool, which is enough for one person.

## Run it as a web service

ChatGPT and hosted Claude connectors need a public HTTPS address. Run the HTTP transport:

```bash
cp .env.example .env   # then edit
uv run opencivicquery-server
```

The MCP endpoint is `/mcp` and the health check is `/health`. The `Dockerfile` builds an image for Cloud Run or any container host.

## Configuration

A deployment file in `deployments/` sets:

| Setting | Meaning |
|---|---|
| `socrata.portals` | `any` for every Socrata portal, or a list of domains |
| `socrata.app_token_env` | Environment variable that holds a Socrata app token. Use one on any shared instance. |
| `limits.daily_requests` | Calls that reach a data source or read a snapshot, per UTC day, per server process |
| `limits.per_portal_per_minute` | Calls to one portal in any rolling minute, per server process |
| `limits.per_client_daily` | Calls one client may make per UTC day, per server process. A client is one IPv4 address or one IPv6 /64. Leave it out for a local install. |
| `limits.client_address` | Required with `per_client_daily`. `peer` reads the connection address; `x-forwarded-for-last` reads the last `X-Forwarded-For` entry. Use the second only behind exactly one proxy that appends the client address, as Cloud Run does. |
| `limits.shared_networks`, `limits.shared_networks_files` | Address ranges that carry many users at once, such as hosted AI connectors. Their calls skip the per-client cap and meet only the server-wide caps. Files use the `{"prefixes": [...]}` format that OpenAI publishes. |
| `packs` | Source pack files to load |
| `access` | Who may sign in, used only when login is on |

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `OPENCIVICQUERY_CONFIG_PATH` | required | Deployment file |
| `OPENCIVICQUERY_AUTH_MODE` | `open` | `open` (no login) or `oauth` (Google sign-in) |
| `OPENCIVICQUERY_TRANSPORT` | `http` | `http` or `stdio` |
| `OPENCIVICQUERY_HOST` | `127.0.0.1` | HTTP bind address. The Docker image sets `0.0.0.0`. |
| `OPENCIVICQUERY_SNAPSHOT_BACKEND` | `local` | `local` or `gcs` |
| `OPENCIVICQUERY_SNAPSHOT_PATH` | `~/.opencivicquery/snapshots` | Folder for local snapshots |
| `OPENCIVICQUERY_SNAPSHOT_BUCKET` | | Bucket for Cloud Storage snapshots |

Login is off by default. A newsroom that wants per-reporter attribution can turn on Google sign-in; see `deployments/newsroom-with-login.example.yaml` and the OAuth variables in `.env.example`. A deployment file with an `access` section refuses to start while login is off, so a forgotten setting never opens a private instance to everyone.

The usage caps count per process. A deployment that runs two instances admits up to twice the configured numbers, so set the platform's instance ceiling as the outer bound on cost.

The per-client cap raises the cost of abuse; it does not prevent it. Someone with ten addresses, or one IPv6 block, can still use up the server-wide daily allowance. The server-wide cap and the instance ceiling are what bound cost.

## Contributing a source pack

1. Pick datasets reporters ask about. Find them with the `search_datasets` tool or on the portal.
2. Check each dataset against live data before you write anything: what one row represents, whether a unique key holds, which time field to use, and which values are missing or odd.
3. Write the profile: `row_grain`, `counting_guidance`, and `caveats`. Every statement must come from the agency's own dataset or field description, or from a live query you ran, and nothing you inferred goes in. Put the basis in a comment beside each statement: `# official: <where>` or `# checked <date>: <what the query showed>`. A test fails when a statement has no basis. When the data contradicts the documentation, record what the query showed.
4. Set `checked_on` to the date you checked.
5. Add the pack to a deployment file and run the tests.

## Plugin development

The skill lives in `plugins/opencivicquery/skills/ask-open-data/`. Both the Claude plugin and the ChatGPT package use it, and the version in `plugins/opencivicquery/.claude-plugin/plugin.json` applies to both. Raise that version with every change to the skill or the connection.

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests
```

After you change dependencies, regenerate the pinned requirements:

```bash
uv export --frozen --no-dev --no-emit-project --format requirements.txt --output-file requirements.txt
```

## License

MIT. See `LICENSE`.
