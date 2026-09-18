# SAS MCP Server — Use-Case Edition

A small [Model Context Protocol](https://modelcontextprotocol.io) server that turns **one SAS Viya
dataset and its ready models** into a focused analytics agent. Point it at your data and models with
a few environment variables and an agent (for example one built in **SAS Retrieval Agent Manager**)
can query the data, score records against the models in real time, and chart the results — and
nothing else.

It is the use-case-scoped sibling of the full
[sassoftware/sas-mcp-server](https://github.com/sassoftware/sas-mcp-server) (92 tools). The plumbing is
ported from there (FedSQL querying, warm compute sessions, readable Viya errors, FastMCP 4); the tool
surface is cut down to the eight tools such an agent needs.

## The tools

| Tool | What it does |
|---|---|
| **get_use_case** | Call first. Reports the use case: the primary table with its columns and row count, and the ready models with their input/output signatures. Grounds the agent in one call. |
| **describe_table** | Row count, column count and every column's name/type/label/format. Defaults to the use-case table. |
| **preview_table** | A page of raw rows, formatted as SAS displays them. |
| **query_data** | A FedSQL `SELECT` over the use-case table(s) returning JSON rows — counts, averages, group-bys, top-N, joins. Bare table names are qualified for you; only a single read-only `SELECT` is accepted; rows are capped server-side. Errors come back as a status dict that names the fix. |
| **render_chart** | Emits an interactive chart spec (`kind: "chart"`; bar/line/area/pie/scatter) from rows you already have — the chat front-end draws it. |
| **list_models** | The published models/decisions the agent may score against, and any allowed ones that are not published yet. |
| **describe_model** | The scoring step and its typed inputs and outputs. |
| **score_data** | Real-time scoring of one record or a list of records against a ready model (SAS Micro Analytic Service). Accepts the model's id **or name**, picks the step (`score`/`execute`) itself, matches input names case-insensitively, converts types, ignores extra keys and reports missing ones — so a `query_data` row can be passed straight in. |

Two MCP prompts (`explore_use_case`, `score_and_explain`) chain the tools for clients that support them.

## Quick start for a SAS Retrieval Agent Manager agent

The published image is

```
ghcr.io/raedaldweik/sas-mcp-usecase:latest
```

(also tagged `:2.0.0` and `:sha-<commit>`; see [Publishing](#publishing)). On first publish a GHCR package
is private — make it public, or give RAM registry credentials, so RAM can pull it.

1. **Data on SAS.** Load your dataset into CAS as a global (promoted) table, for example
   `Public.PATIENTS`.
2. **Model on SAS.** Build the model (Model Studio / Model Manager) and **publish it to SAS Micro
   Analytic Service (MAS)**. Note the published name — that is the module the agent scores with.
3. **Tool server in RAM.** Add a Container MCP server from the image above (or run the container
   anywhere and add a *Remote MCP server* with transport *Streamable HTTP*, URL `http://<host>:8134/mcp`,
   authentication *API Key*). Set these environment variables on it:

   | Variable | Example |
   |---|---|
   | `VIYA_ENDPOINT` | `https://viya.example.com` |
   | `VIYA_REFRESH_TOKEN` (secret) — or `VIYA_USERNAME` + `VIYA_PASSWORD` for non-SSO accounts | see [headless auth](examples/configuration.md#headless-authentication-for-sso-and-federated-environments) |
   | `MCP_API_KEY` (secret) | a long random string; the same key goes on the RAM side |
   | `USE_CASE_NAME` | `Population Health` |
   | `USE_CASE_DESCRIPTION` | `Answers questions about the patient cohort and predicts 30-day readmission risk.` |
   | `ALLOWED_TABLES` | `Public.PATIENTS` |
   | `ALLOWED_MODELS` | `Readmission_GB` |
   | `SSL_VERIFY` | `false` only for a self-signed Viya certificate |

4. **Agent.** Attach the tool server to your agent. A good system prompt starts with "Call
   `get_use_case` first" — after that the agent knows the table, the columns and the model inputs.

Try: *"How many patients over 65 by region? Chart it."* → *"What is the readmission risk for patient
1042?"* (the agent queries the row, then scores it) → *"Score a 71-year-old with 3 prior admissions."*

One image serves many use cases: register it once and create one tool server per use case, each with
its own variables.

## Running it yourself

### Prerequisites
- [Python 3.12+](https://www.python.org/downloads) and [uv 0.8+](https://github.com/astral-sh/uv)
- A SAS Viya environment with the Compute service, and the one-time
  [Viya setup](examples/configuration.md) (OAuth client registration)
- Optional: Docker / Podman

### Install and configure

```sh
git clone https://github.com/raedaldweik/SAS_Use_Case.git
cd SAS_Use_Case
uv sync
cp .env.sample .env      # set VIYA_ENDPOINT, the use case, and credentials
```

### Modes

| | **Direct HTTP** (RAM, server-to-server) | **HTTP** (per-user OAuth) | **Stdio** |
|---|---|---|---|
| Start | `uv run app-http-direct` | `uv run app` | client runs `uv run app-stdio` |
| Auth to Viya | Refresh token or password from `.env`; optional `MCP_API_KEY` on the endpoint | Each user signs in in the browser (PKCE) | Refresh token or password from `.env` |
| Endpoint | `http://host:8134/mcp` (or `/sse` with `MCP_TRANSPORT=sse`) | `http://localhost:8134/mcp` | — |
| Use it for | SAS RAM and other clients that cannot do browser OAuth | Shared multi-user deployments | Local development, Gemini CLI |

**Docker**

```sh
docker build -t sas-mcp-usecase .
docker run --env-file .env -p 8134:8134 sas-mcp-usecase
```

The container defaults to direct HTTP mode (`MCP_MODE=http-direct`); set `MCP_MODE=http` or `stdio`
to change it. `GET /health` is always open for probes.

**Client snippets** for VS Code / Cursor / Claude Code, Claude Desktop and Gemini CLI are in
[`examples/`](examples/) — for example `.vscode/mcp.json`:

```json
{ "servers": { "sas-usecase": { "url": "http://localhost:8134/mcp", "type": "http" } } }
```

## Configuration

All settings are environment variables (or `.env`); the full table is in
[`examples/configuration.md`](examples/configuration.md#environment-file-options). The ones that define
the use case:

| Variable | Purpose |
|---|---|
| `USE_CASE_NAME` / `USE_CASE_DESCRIPTION` | Identify the use case (returned by `get_use_case`) |
| `ALLOWED_TABLES` | The dataset(s). The **first** entry is the primary table the data tools default to. Each entry: `table`, `caslib.table` or `server.caslib.table` |
| `ALLOWED_MODELS` / `ALLOWED_DECISIONS` | Published MAS module ids or names the agent may score against (both feed one allowlist) |
| `DEFAULT_CAS_SERVER` / `DEFAULT_CASLIB` | Used when a table entry omits them (`cas-shared-default` / `Public`) |
| `SCOPE_ENFORCE` | `true` (default) blocks out-of-scope tables and models; `false` only hides them |

With none of the `ALLOWED_*` variables set the server is unscoped: every tool works, tables must be
named explicitly, and any published model can be scored.

## How it works

- **Querying.** `query_data` screens the statement (single `SELECT`, no macro triggers, balanced quotes —
  an unterminated literal would wedge the session), qualifies bare table names against `ALLOWED_TABLES`
  and refuses qualified names outside it, then runs FedSQL in a **warm compute session** that is kept
  per user and reused, so a query takes about a second instead of paying a session start each call.
  Every request in direct-HTTP mode carries the same service-account token, so many chat users share one
  session: a per-session lock serialises their jobs (a compute session runs one job at a time). A job that
  overruns `JOB_POLL_TIMEOUT` is abandoned and its session discarded so nothing can block later calls.
  Rows are read back from a format-stripped copy (numbers stay numbers, missings are `null`, dates become
  ISO text) and capped at `limit` with a `truncated` flag.
- **Scoring.** `score_data` resolves the module by id or display name (cached), lists its steps and
  prefers `score` (models) over `execute` (decisions), maps the record onto the declared inputs and posts
  to MAS. Nothing is persisted.
- **Errors** from Viya are quoted verbatim (`HTTP 404 from GET /… — Viya reported: …`) instead of a bare
  status code, so the agent can correct itself.
- **Tool annotations.** Every tool advertises `readOnlyHint`; only `score_data` is not read-only.

## Testing

```sh
./run_tests.sh                       # unit tests (no Viya needed)
./run_tests.sh --integration         # + end-to-end against VIYA_ENDPOINT/VIYA_USERNAME/VIYA_PASSWORD
uv run ruff check src tests          # lint
```

The integration tests target the SAS sample table `Public.HMEQ` and skip what is not present.

## Publishing

`.github/workflows/build-and-push.yml` builds a multi-arch image (amd64 + arm64) and pushes it to
`ghcr.io/<owner>/sas-mcp-usecase` on every push to `main`, on every `v*` tag, and on **Run workflow**
from the Actions tab (any branch). Each build pushes `:latest`, `:<version from pyproject.toml>` and
`:sha-<commit>`; a `v*` tag adds the semver tags. `ci.yml` runs lint, the unit tests and a wheel build on
every push and pull request.

The Python package builds with `uv build` (`sas-mcp-usecase`, import name `sas_mcp_server`) and can be
installed straight from git: `pip install git+https://github.com/raedaldweik/SAS_Use_Case.git`.

## Relationship to sassoftware/sas-mcp-server

This repository tracks the upstream server's internals but not its surface. Ported from upstream 1.15.0:
the FedSQL query engine and its error mapping, the compute-session pool, `raise_for_viya_status`, the
`SSL_VERIFY=false` patch (httpx and httpx2), MCP tool annotations and the FastMCP 4 migration. Deliberately
not included: SAS code execution, AutoML, reports, batch jobs, decisioning authoring, the glossary, and MCP
Apps views — a use-case agent does not need them, and a smaller tool set keeps it reliable.

## License & Attribution

Except for the contents of the /static folder, this project is licensed under the
[Apache 2.0 License](LICENSE). Elements in the /static folder are owned by SAS and are not released under
an open source license. SAS and all other SAS Institute Inc. product or service names are registered
trademarks or trademarks of SAS Institute Inc. in the USA and other countries. ® indicates USA registration.

Separate commercial licenses for SAS software (e.g., SAS Viya) are not included and are required to use
these capabilities with SAS software. All third-party trademarks referenced belong to their respective
owners.

This project uses Python ([license](https://docs.python.org/3/license.html)), FastMCP (Apache 2.0),
uvicorn (BSD 3-Clause), starlette (BSD 3-Clause) and httpx (MIT).
