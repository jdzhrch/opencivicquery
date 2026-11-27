# OpenCivicQuery

OpenCivicQuery is an MCP server that lets reporters ask questions of government open data in plain language, from Claude, ChatGPT, or any other MCP client. It reaches every portal on the Socrata platform: cities, counties, states, and federal agencies. It also places any U.S. street address in census geography through the Census Bureau's Census Geocoder.

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

Packs cover San Francisco, New York City, and Cook County, Illinois. The court-related profiles include New York City's administrative hearing summonses (OATH), Housing Court cases of the city's housing agency (HPD Housing Litigations), and executed evictions, and the Cook County State's Attorney's intake, disposition, and sentencing records, which the office stopped updating at the end of 2024.

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

## Census Geocoder

The `geocode_address` tool sends one U.S. street address to the [Census Geocoder](https://geocoding.geo.census.gov/geocoder/) and returns each matched address with approximate coordinates and the census geographies that contain it: state, county, county subdivision, place, census tract, 2020 census block, and congressional and state legislative districts. It works nationwide and needs no key. Reporters use it to join an address in a records query to census figures for its tract or county.

Its answers carry the same evidence as a dataset query: a public link that reruns the lookup, warnings, presentation rules, and a snapshot. The warnings come from the Census Bureau's own documentation or a dated live check:

- The coordinates are interpolated along the street's address range, so they are approximate.
- The geocoder can match an address spelled differently from the input, and a short address can match several places. When it does, the assistant asks which one is meant.
- No match does not mean the address does not exist. The Census Bureau lists non-residential addresses, new construction, changed addresses, missing address ranges, demolished housing, and confidentiality under Title 13 as causes.
- The geographies are the current ones. Data from an earlier survey or census may use different boundaries.
- District layers name their own term, such as the 120th Congress, and can describe districts for a term that has not begun. The assistant names the layer beside any district.

The tool uses the current benchmark and vintage only. Addresses are limited to 100 characters, the geocoder's own limit. Lookups count toward the same usage caps as queries.

## Census data

`get_census_data` pulls American Community Survey 5-year estimates from the Census Bureau's [Census Data API](https://www.census.gov/data/developers/data-sets/acs-5year.html) for any geography the API serves, from a single county to every tract in one. Each answer keeps the API's own values and labels, names the margins of error and annotation codes a reader must not drop, and freezes the result as a citable snapshot. The server needs a free Census Data API key in `OPENCIVICQUERY_CENSUS_API_KEY`; the key is never part of a link, a snapshot, or a log line, so a reader who reruns a query adds their own. This product uses the Census Bureau Data API but is not endorsed or certified by the Census Bureau.

## Meeting records from Legistar

Many cities and counties publish their meetings through Granicus [Legistar](https://webapi.legistar.com/Home/Examples). Four tools read its public Web API, which most jurisdictions leave open without a key:

- `find_meetings` lists the meetings of a council, committee, board, or commission in a date range, with agenda and minutes PDF links, the minutes status, and the meeting page.
- `get_meeting_items` lists one meeting's agenda items with the action, result, tally, mover, and seconder the clerk recorded.
- `get_item_votes` lists each member's recorded vote on one item.
- `search_legislation` finds ordinances, resolutions, and reports by words in the title or by file number, with a link to each one's page.

The caller names a Legistar client, the first part of the jurisdiction's Legistar address, such as `oakland` for oakland.legistar.com. Requests go to `webapi.legistar.com`; the client name is checked against a strict pattern before it becomes part of the path. For a client whose API copy is stale or cannot answer, the tools also read that client's own public pages, as described below. Answers carry the same evidence as a dataset query, and the warnings come from the Legistar documentation or a dated live check: the API returns only records the clerk marked public, records can change after retrieval and minutes carry a status such as DRAFT, an empty result or tally does not mean nothing happened, and the legislation search matches text inside longer words. Replies are capped at 1,000 records, Legistar's own limit, and a full page is reported as truncated.

Some jurisdictions, including New York City and Philadelphia, require their own Legistar token. A server can hold one per jurisdiction in `OPENCIVICQUERY_LEGISTAR_TOKEN_<CLIENT>`, such as `OPENCIVICQUERY_LEGISTAR_TOKEN_NYC`; it goes only into the outgoing request, never into a link, a snapshot, or a log line, and the answer says that a reader needs a token of their own to rerun the link. The tools return the agenda and minutes links and leave meeting video and captions to the jurisdiction's own site.

A jurisdiction's API copy can stop updating while its website stays current. On 2026-11-21 the newest matter in San Francisco's API (client `sfgov`) was introduced on 2020-09-17, and its events resource answered every request with a settings error. So `search_legislation` also asks for the client's newest matter. When that is more than 180 days old, or the API answers with a settings error, the tools read the client's public Legistar pages instead and say so first in the answer. When the pages cannot be read, `search_legislation` returns the API's older copy under a warning that names the date it stops and says to try again, and the other tools return an error that says the site could not be read just now.

For such a client the tools read four more page types on `https://<client>.legistar.com`. The Web API stays the source everywhere else.

- `search_legislation` runs the site's advanced search on the title and the introduction dates and, when the text holds a digit, on the file or enactment number as well. It returns each matter's file number, type, status, title, and introduced and final-action dates. It then opens the pages of the first ten matters for their sponsors. A search for one file number also returns that matter's action history: the date, body, action, and result of each step, with the IDs of its meeting and its action.
- `get_meeting_items` reads one meeting's page, which has one row for each action recorded on a legislative file, with file number, action, and result. A file with two actions at the meeting fills two rows, so the answer says to count files by distinct file number. It needs the meeting's GUID as `event_guid`, which `find_meetings` and a matter's action history both return.
- `get_item_votes` reads the meeting's page to confirm that the item belongs to it, then reads the action's page for each member's vote.

The limits are stated in each answer. The search page shows 100 matters per view and the tool reads the first view, so a search with more matches is marked truncated and the answer gives the page's own count. Matter pages past the tenth are not read, and each row says in `DetailPageRead` whether its page was. The search page cuts titles at about 600 characters. The meeting page says which view it shows; in the "Legislation only" view, procedural items stay in the agenda file. A page built differently from the ones checked, such as a grid without the expected columns or an action page without a vote table, is treated as unread and never as an empty result. Page reading has a 25-second budget inside a 50-second budget for the whole call, and each server process reads pages for one call at a time. Requests carry the server's own `OpenCivicQuery/<version> (+https://github.com/jdzhrch/opencivicquery)` identity and follow no redirect. An ID or GUID taken from a page is checked against a strict pattern before it is used, and no other address from a page is ever requested. On 2026-11-26 a search for San Francisco housing matters introduced in November 2026 returned 10 with their sponsors, file 260875 returned its six actions, its final passage returned 11 member votes, and the Rules Committee meeting of 2026-09-14 returned 7 recorded actions on 5 files.

The request sequence and the page landmarks are adapted from [python-legistar-scraper](https://github.com/opencivicdata/python-legistar-scraper), under its BSD license; the notice is kept in `src/opencivicquery/sources/legistar_pages.py`. The library is not a dependency: it turns off certificate checks, follows redirects, and brings its own HTTP stack, which this server's host rules do not allow.

When the events resource answers with such a settings error, `find_meetings` reads the client's public calendar page, `https://<client>.legistar.com/Calendar.aspx`, instead. The Web API has just confirmed the client by naming its setting, so the server reads only that client's own Legistar site. The page takes its year, body and sort order from cookies that the site itself sets, shows 100 meetings per view, and lists each meeting's agenda and minutes files and meeting page. The tool reads at most two calendar years per call, up to the page's Next Year view, each year newest first and, when that view may continue past the start of the range, oldest first. A view counts as possibly continued when it has a pager or a full page of rows. Dates that neither view reaches, and dates past next year, are named in a warning, and the result is then marked truncated. The calendar page does not give agenda items, member votes, or the minutes status. Each meeting row carries its `EventId` and `EventGuid`, which `get_meeting_items` and `get_item_votes` take to read the meeting's own page. Only links to the client's own Legistar site are kept. The tool returns the API's settings error instead when the page cannot be read, shows a different year, body selection or order than the one asked for, has a malformed row, or takes longer than its own 25-second budget, which ends before the service's deadline. Each server process reads one calendar at a time, so a public instance does not load a city's site with parallel page requests. On 2026-11-22 this read 13 San Francisco meetings for November 2026 and 181 for all of 2026, the total the page's pager gave.

## SEC filings

`find_sec_filings` lists one company's filings from the SEC's [EDGAR submissions API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces), newest first, with links to each filing index and main document. It accepts a ticker, a CIK, or an exact EDGAR company name, and when a name is not exact it returns candidates with their CIKs instead of guessing. The SEC asks every automated client to declare a contact, so set `OPENCIVICQUERY_SEC_CONTACT`; without it the tool refuses to run. Requests are spaced to stay under the SEC's limit of 10 a second.

The warnings come from the SEC's own pages or a dated live check: EDGAR's recent block holds at least a year or 1,000 filings, and a search that reaches further back is reported as partial; a company's list includes filings others submit about it, such as insiders' Form 4; form types match exactly, so 10-K leaves out 10-K/A; and a related company, such as a predecessor, can file under its own CIK. A row shows that a filing exists and says nothing about what it says.

## Federal awards

`search_federal_awards` searches federal prime awards on [USAspending.gov](https://www.usaspending.gov/): contracts, grants, loans, direct payments, and other assistance, by keyword, recipient, or the state and county where the work is done. Each result keeps USAspending's own field names and links every award to its public page. The source answers only POST requests, so the response records the exact request body in place of a rerunnable link. The warnings state the official caveats that change how the numbers read: an award appears when it had activity in the date range, amounts are obligations for the whole award, Defense contracts are published 90 days late, and subawards are not included.

## California Legislature bills

Two tools read bills of the California Legislature from [Open States](https://docs.openstates.org/api-v3/), which compiles each state legislature's own records. `search_california_bills` finds bills in one session by words, by the member who sponsored them (as an author, or as any sponsor), or by an action on or after a date, and returns Open States' count of matches with the first 20, most recent action first. `get_california_bill` returns one bill's authors and coauthors, every recorded action, and each roll call with its tally and how each member voted. Every row links the bill's page on the Legislature's site, leginfo.legislature.ca.gov, which is the record. Without a session the tools use the most recent session Open States lists, and say so.

The API needs a free key in `OPENCIVICQUERY_OPENSTATES_API_KEY`, which the server sends only in a request header and never in a link or a snapshot. A free key allows 250 requests a day, counted in UTC, and 10 a minute, for the whole server, so a lookup sends at most two: the bill search or the bill, and, once every six hours per process, California's list of sessions with the time Open States last updated a California bill, which answers give as their update time. Each server process also keeps to 10 requests a minute itself, and refuses the eleventh without sending it. When the day's allowance is used up, the tools say it resets at midnight UTC. The warnings come from Open States' documentation or a dated live check: a sponsor name is compared exactly, the action date filter has no end date, option other in a roll call counts members who did not vote yes or no, and a roll call is dated by its day in California, so an evening floor vote that Open States records after midnight UTC keeps its California date. Open States' hearing records for California were checked on 2026-11-27 and held none for the first half of April 2026, a busy month for committee hearings, so no hearing tool is offered.

## Nonprofit tax filings

`search_nonprofits` finds tax-exempt organizations by name in ProPublica's [Nonprofit Explorer API](https://projects.propublica.org/nonprofits/api), which republishes the IRS list of exempt organizations, and returns each match's EIN, city, and state. `get_nonprofit_filings` lists one organization's Form 990, 990-EZ, and 990-PF filings, newest tax period first, with total revenue, expenses, assets, and liabilities where the IRS annual extract holds them, and a link to each filed document. The API needs no key. The warnings come from ProPublica's documentation or a dated live check: the search also matches city names, a tax period is the month the fiscal year ended, the summary figures come from different lines on each form type, and the latest years often have only the filed document. ProPublica's data terms require publications that use the data to cite ProPublica, so every answer carries that rule.

## San Francisco's city website

Four tools read the content interface behind [SF.gov](https://www.sf.gov/), San Francisco's city website, at `api.sf.gov`. It needs no key.

- `find_sfgov_news` lists one agency's news pages, or every agency's, in a date range, with each page's date, type label, summary, and public link.
- `find_sfgov_meetings` lists the meeting pages of a board, commission, or department for a date range, with the page link, which carries the agenda, the files staff attached with their headings, such as minutes, and video links.
- `search_sfgov` finds pages by words in the title, or files in the document library by words in the title or description.
- `get_sfgov_page` returns the text of one news or meeting page: a news page's body, or a meeting page's overview and agenda items.

The caller names an agency exactly as SF.gov does, such as `Office of the Mayor` or `Police Commission`. A name that is not exact returns the names that match, and the tool never picks one. This project found no public documentation for the interface, so every caveat comes from a live check on 2026-11-26, and each answer says so. The interface cannot filter by a date range. The news tool therefore reads the 500 newest items and reports a range it could not reach as partial. The meeting tool reads up to 496 of a body's pages and keeps the ones whose dates reach into the range; a page that covers a span of days, such as a notice that no meeting takes place for several weeks, is kept for any day of the span, and a page with no readable date is listed last in every answer with a warning. SF.gov has no minutes field, so the tool returns the attached files under the headings staff wrote and leaves it to the reader to check which file is the minutes of that meeting. A title search cannot be limited to one agency. The list and search tools read only English pages; `get_sfgov_page` reads a translated page when given its page_id and says so. Its text leaves out the files attached to single agenda items, which stay on the public page. The Board of Supervisors and its committees publish their meetings through Legistar, so use `find_meetings` with client `sfgov` for those.

The server reads the content interface behind www.sf.gov on one host, `api.sf.gov`, and identifies itself as OpenCivicQuery. It returns links to documents, public pages, and video without requesting them. Each server process runs one SF.gov lookup at a time with at most one request a second; a lookup that waits more than 20 seconds behind others is refused before it sends anything. When the site answers HTTP 429, the tool retries nothing, that process sends the site nothing for the next 60 seconds, and the answer comes from the daily copy where the process has one, or is an error that says to try again in a minute. These limits hold per process, so a deployment with two instances can send twice as much.

### The daily copy on the public instance

On the public instance, the four SF.gov tools answer from a daily copy. The copy holds the English news pages, with their text, and the meeting pages, with their agenda text and attached files, of nine agencies: the Office of the Mayor, the Controller's Office, the Board of Supervisors, the Department of Public Health, the City Attorney, Homelessness and Supportive Housing, the Police Commission, the San Francisco Health Commission, and the Board of Appeals. Every answer from the copy names the date the copy was taken and the agencies it covers, says what it leaves out, and is marked limited, never complete. It ends with a sentence that points to the Claude Desktop extension for live results that cover every agency. An agency outside the copy gets an error that lists the agencies it covers.

A maintainer's computer makes the copy once a day with `opencivicquery sfgov-copy` and writes it to the server's snapshot storage under `sfgov-copy/`. It reads with the same identity and pace as the tools, one request a second. After the first run it fetches only news pages that are new or whose listing changed, so a daily run takes about 50 requests. `operations/sfgov-copy.md` is the runbook.

## SF.gov on Claude Desktop

The OpenCivicQuery SF.gov extension runs the four SF.gov tools, and the two snapshot tools, on your own computer, so answers are live and cover every agency on SF.gov. It needs Claude Desktop on macOS or Windows and nothing else: Claude Desktop installs the extension's Python and packages itself. Evidence snapshots are saved under `~/.opencivicquery/snapshots`.

To install it:

1. Download [`opencivicquery-sfgov.mcpb`](https://github.com/jdzhrch/opencivicquery/releases/latest/download/opencivicquery-sfgov.mcpb), the file of the latest release, or build it as shown below. Every version keeps this file name; its version is inside the file.
2. Double-click the file, or drag it into the Claude Desktop window. You can also open Settings, Extensions, Advanced settings, Install Extension, and choose the file.
3. Click Install, then ask a question about an SF.gov agency, such as "What did the Mayor's office announce last week?"

The extension is not packed into the Claude plugin. On 2026-11-27 Claude Code 2.1.332 loaded only `python`, `node`, and `binary` bundles from a plugin, so a plugin copy would need uv installed on each computer, and chat on the web, in the desktop app, and on mobile does not load a plugin's local servers. The plugin's skill tells the assistant to prefer the extension's live tools whenever they are present.

A Claude Team or Enterprise owner can offer it to every member: open Organization settings, Connectors, and the Desktop tab; click Add custom extension and choose the file; then open its menu under Custom team extensions and choose Add to team. Members then install it with one click. For a new version, raise the version in `extensions/sfgov-desktop/manifest.json`, rebuild, and use Upload new version from the same menu.

To build the file from this repository:

```bash
uv run python scripts/build_sfgov_extension.py --validate --smoke
```

The file lands in `dist/` as `opencivicquery-sfgov.mcpb`. `--validate` checks the manifest with the official MCPB tool and needs Node.js; `--smoke` unpacks the file, starts its server, and lists its tools. Each version is published as a release tagged `sfgov-desktop-v<version>`, with the file attached under exactly that name, and that release must be the repository's Latest release: answers from the public instance's daily copy link to `releases/latest/download/opencivicquery-sfgov.mcpb`. Publish any other release with `--latest=false`, and publish the extension's release before deploying a server that links to it.

Every tool except `pin_snapshot` declares itself read-only, so clients such as Claude's Cowork can run it without asking each time. A lookup still saves an evidence snapshot: on the hosted server, temporary snapshots are deleted after 90 days, while the extension keeps them under `~/.opencivicquery/snapshots` until you delete them.

## Federal court opinions

`search_court_opinions` searches the [United States Courts Opinions](https://www.govinfo.gov/app/collection/uscourts) collection on GovInfo, which holds opinions from selected federal courts, generally from 2004 on. Each search returns GovInfo's count of matching opinion documents, the top-ranked page with public GovInfo links, and caveats about coverage, ranking, and dates. It needs a free api.data.gov key in `OPENCIVICQUERY_GOVINFO_API_KEY`, which the server sends only in a request header and never in a link or a snapshot.

## Install

A public instance runs without login at `https://opencivicquery-1040721507408.us-west1.run.app/mcp`. The plugin connects to it and adds the **Ask Open Data** skill, which teaches the assistant how to turn a reporter's question into a countable one, compare periods, and hand back evidence an editor can check.

### Claude: the plugin

The plugin has three parts: the skill, a connection to the server, and a connection to [CourtListener](https://www.courtlistener.com/help/mcp/)'s official connector for court records. Claude installs the skill with the plugin, but the OpenCivicQuery connection has to exist as a connector before the plugin can use it. CourtListener's connector is listed in Claude's connector directory; each person signs in to it with a free CourtListener account the first time it is used. CourtListener's terms forbid sharing one account's tokens across users, so the server does not proxy it.

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

The hosted instance caps each direct caller, such as Claude Code or a script, at 200 calls a day per IP address, on top of a server-wide daily cap. Claude and ChatGPT connectors reach the server from the vendors' own addresses on behalf of many users, so their calls count only toward the server-wide cap. Audit logs record a salted hash of the caller's IP address, never the IP address itself. They do record the street addresses sent to the geocoder, as they record every query. For heavy use, run your own copy.

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

This is a query-only service. Independent GET event streams at the MCP endpoint return HTTP 405 with `Allow: POST`, including requests from older clients. Normal POST initialization and tool calls still work; health checks, OAuth discovery, and the local stdio transport are unchanged. The service does not provide idle change-notification subscriptions. A server that scales to zero may take longer to answer the first request after an idle period.

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
| `OPENCIVICQUERY_SEC_CONTACT` | | Name and email the SEC asks every automated client to declare. Without it, `find_sec_filings` refuses to run. |
| `OPENCIVICQUERY_CENSUS_API_KEY` | | Free Census Data API key. Without it, the census data tool refuses to run. |
| `OPENCIVICQUERY_LEGISTAR_TOKEN_<CLIENT>` | | Legistar token for one jurisdiction that requires one, such as `OPENCIVICQUERY_LEGISTAR_TOKEN_NYC`. Underscores in the client name stand for hyphens. |
| `OPENCIVICQUERY_GOVINFO_API_KEY` | | Free api.data.gov key for GovInfo. Without it, the court opinions tool refuses to run. |
| `OPENCIVICQUERY_OPENSTATES_API_KEY` | | Free Open States key. Without it, the California bill tools refuse to run. |
| `OPENCIVICQUERY_TOOLS` | | Comma-separated tool names. When set, the server offers only these tools. The Claude Desktop extension uses it. |

Login is off by default. A newsroom that wants per-reporter attribution can turn on Google sign-in; see `deployments/newsroom-with-login.example.yaml` and the OAuth variables in `.env.example`. A deployment file with an `access` section refuses to start while login is off, so a forgotten setting never opens a private instance to everyone.

The usage caps count per process and reset when that process restarts. Concurrent instances have separate counters, and repeated restarts can reset the allowance again within the same day. The platform's instance ceiling limits concurrent capacity; it does not impose a hard daily usage or spending limit.

The per-client cap raises the cost of abuse; it does not prevent it. Someone with ten addresses, or one IPv6 block, can still use up the server process's daily allowance. Monitor actual usage and billing rather than treating these caps as a guaranteed budget.

## Contributing a source pack

1. Pick datasets reporters ask about. Find them with the `search_datasets` tool or on the portal.
2. Check each dataset against live data before you write anything: what one row represents, whether a unique key holds, which time field to use, and which values are missing or odd.
3. Write the profile: `row_grain`, `counting_guidance`, and `caveats`. Every statement must come from the agency's own dataset or field description, or from a live query you ran, and nothing you inferred goes in. Put the basis in a comment beside each statement: `# official: <where>` or `# checked <date>: <what the query showed>`. A test fails when a statement has no basis. When the data contradicts the documentation, record what the query showed.
4. Set `checked_on` to the date you checked.
5. Add the pack to a deployment file and run the tests.

## Plugin development

The skill lives in `plugins/opencivicquery/skills/ask-open-data/`. Its reference `where-else-to-look.md` lists paid and manual-only channels for records the tools cannot reach, such as state court dockets and meeting transcripts, with what each holds, how to get access, and its published price; for some San Francisco websites it gives the steps a reporter follows by hand, and for others it names the tool or dataset that already holds the same records. Every entry names a dated source, and a test enforces it. A second reference, `san-francisco-request-channels.md`, lists the records-request and press channels of 53 San Francisco agencies, role-based only, each with the official page it came from and the date it was checked. Both the Claude plugin and the ChatGPT package use it, and the version in `plugins/opencivicquery/.claude-plugin/plugin.json` applies to both. Raise that version with every change to the skill or the connection.

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

MIT. See `LICENSE`. The Legistar page reading adapts parts of python-legistar-scraper, whose BSD license notice is reproduced in `src/opencivicquery/sources/legistar_pages.py`.
