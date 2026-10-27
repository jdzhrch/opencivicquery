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

Current packs: San Francisco (`packs/us-ca-san-francisco.yaml`) and New York City (`packs/us-ny-new-york-city.yaml`). To add a city, see [Contributing a source pack](#contributing-a-source-pack).

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

## Contributing a source pack

1. Pick datasets reporters ask about. Find them with the `search_datasets` tool or on the portal.
2. Check each dataset against live data before you write anything: what one row represents, whether a unique key holds, which time field to use, and which values are missing or odd.
3. Write the profile: `row_grain`, `counting_guidance`, and `caveats`. Write only what the data or the agency's documentation supports. When the data contradicts the documentation, say so.
4. Set `checked_on` to the date you checked.
5. Add the pack to a deployment file and run the tests.

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
