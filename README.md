# SAS MCP Server — Use-Case Edition

A Model Context Protocol (MCP) server that turns a single SAS Viya dataset (plus
its associated models and decisions) into a focused analytics assistant. Point
it at your data with a few environment variables and the agent becomes an expert
on that dataset — querying it, charting it, scoring against ready models, and
building new ones — without being distracted by the rest of the environment.

> This is the **use-case-scoped** edition with a small, purpose-built tool set.
> For the full SAS Viya copilot (the complete tool surface), use the upstream
> SAS MCP server.

## Features

- Query a scoped dataset with SAS or SQL and get back structured rows
- Render interactive charts from query results
- Score records in real time against ready models and decisions
- Build ML models with AutoML and read back their performance
- OAuth2 authentication with PKCE flow (plus headless refresh-token / direct modes)
- HTTP-based MCP server compatible with MCP clients

## Getting Started
### Prerequisites
- Required
    - [Python 3.12+](https://www.python.org/downloads) 
    - [uv 0.8+](https://github.com/astral-sh/uv)  
    - [SAS Viya environment](https://www.sas.com/en_us/software/viya.html) with compute service
    - Setup the Viya environment for MCP
        - See [configuration.md](/examples/configuration.md)

- Optional
    - [Docker](https://docs.docker.com/engine/install): refer to [docker setup](/examples/docker/setup.md)

### Installation

1. Clone the repository:
```sh
git clone <repository-url>
cd sas-mcp-server
```

2. Install dependencies
```sh
uv sync
```

NOTE: This will by default create a virtual environment called .venv in the project's root directory. 

If for some reason the virtual environment is not created, please run `uv venv` and then re-run `uv sync`.

### Usage

1. Configure environment variables:
```sh
cp .env.sample .env
```

Edit `.env` and set
```sh
VIYA_ENDPOINT=https://your-viya-server.com
```

2. Start the MCP server (see [Choosing a deployment mode](#choosing-a-deployment-mode) below):

**Option A: HTTP mode** (pre-run the server, connect from MCP client)
```sh
uv run app
```
The server will be available at `http://localhost:8134/mcp` by default. Authentication is handled via OAuth2 PKCE flow in the browser.

**Option B: Stdio mode** (MCP client starts the server on demand)

Set `VIYA_USERNAME` and `VIYA_PASSWORD` in your `.env` file, then configure your MCP client to launch the server directly (see below). For **SSO/federated environments (e.g. Okta)** the password grant does not work — set `VIYA_REFRESH_TOKEN` instead (see [Headless authentication for SSO environments](examples/configuration.md#headless-authentication-for-sso-and-federated-environments)).

**Option C: Direct HTTP mode** (long-running server, no browser OAuth — for server-to-server MCP clients such as SAS Retrieval Agent Manager)

Set `VIYA_USERNAME` and `VIYA_PASSWORD` — or, for SSO/federated environments and unattended 24/7 use, `VIYA_REFRESH_TOKEN` (see [Headless authentication for SSO environments](examples/configuration.md#headless-authentication-for-sso-and-federated-environments)) — (and optionally `MCP_API_KEY`) in your `.env` file, then:
```sh
uv run app-http-direct
```
The server authenticates to Viya itself with the `.env` credentials and serves streamable HTTP at `http://host:8134/mcp` (or SSE at `http://host:8134/sse` with `MCP_TRANSPORT=sse`). If `MCP_API_KEY` is set, clients must send it as an `X-API-Key` header or `Authorization: Bearer` token.

**Option D: Docker / Podman** (containerized deployment)
```sh
docker build -t sas-mcp-usecase .
docker run -e VIYA_ENDPOINT=https://your-viya-server.com -p 8134:8134 sas-mcp-usecase
```

A GitHub Actions workflow (`.github/workflows/build-and-push.yml`) builds and
publishes this image to GitHub Container Registry on every push to `main`, as
`ghcr.io/<owner>/sas-mcp-usecase:latest`. This is a **separate** package from the
full-copilot server (`sas-mcp-server`), so the two never overwrite each other.
On first publish the package is private — make it public (or give SAS Retrieval
Agent Manager registry credentials) so RAM can pull it.

### Choosing a deployment mode

| | **HTTP** | **Stdio** | **Direct HTTP** | **Docker** |
|---|---|---|---|---|
| **How it runs** | Long-running server you start separately | MCP client spawns it on demand | Long-running server you start separately | Containerized HTTP server |
| **Authentication** | OAuth2 PKCE flow (browser popup) | Password grant, or refresh token for SSO (in `.env`) | Password grant, or refresh token for SSO (in `.env`); optional API key on the endpoint | OAuth2 PKCE flow (browser popup) |
| **Best for** | Multi-user or shared setups; production-like environments | Single-user local development; quick experimentation | Server-to-server MCP clients that cannot do browser OAuth (e.g. SAS Retrieval Agent Manager) | Team deployments; CI/CD; environments without Python installed |
| **Requires** | Python + uv | Python + uv | Python + uv | Docker or Podman only |
| **Credentials stored?** | No — user authenticates interactively | Yes — username/password or refresh token in `.env` | Yes — username/password or refresh token in `.env` | No — user authenticates interactively |
| **MCP client config** | Point client to `http://localhost:8134/mcp` | Client runs `uv run app-stdio` | Point client to `http://host:8134/mcp` (+ API key if set) | Point client to `http://host:8134/mcp` |

**Quick guidance:**
- **Starting out or exploring?** Use **stdio** — zero setup beyond `.env`, and your MCP client manages the server lifecycle.
- **Need secure, interactive auth?** Use **HTTP** — no stored passwords, each user authenticates via browser.
- **Deploying for a team or on a server?** Use **Docker** — portable, no Python dependency on the host, easy to integrate with orchestrators.
- **Using Gemini CLI?** Use **stdio** — Gemini CLI does not support HTTP mode or browser-based OAuth. See [Gemini CLI configuration](examples/configuration.md#gemini-cli).
- **Connecting from SAS Retrieval Agent Manager (RAM)?** Use **direct HTTP** — in RAM, add a *Remote MCP server* with transport *Streamable HTTP*, URL `http://<host>:8134/mcp`, and authentication *API Key* (matching `MCP_API_KEY`) or *None*. If your Viya uses **SSO/Okta**, authenticate the server to Viya with `VIYA_REFRESH_TOKEN` (set it as a secret on the tool server's Environment Variables tab) rather than a username/password — see [Headless authentication for SSO environments](examples/configuration.md#headless-authentication-for-sso-and-federated-environments).

### Available Tools

This server is intentionally focused on a single use case (one dataset plus its
associated models/decisions), so it exposes a small, purpose-built tool set —
**14 tools** — rather than the full SAS Viya surface. A focused tool set keeps
the agent a reliable expert on its data instead of overwhelming it with choices.
The data tools default to the use-case table, so you rarely pass table names.

#### Use case & grounding
- **get_use_case**: Report the use case — the primary dataset (with its columns), and the models/decisions this assistant may use. Call this first.

#### Querying & code execution
- **execute_sas_code**: Execute arbitrary SAS code and retrieve the log and listing (data prep, PROC-based modelling, assessment, any SAS step)
- **query_table**: Run a SQL SELECT and get back **structured rows** (columns + rows) — the right tool for "top N…" questions you then want to chart
- **get_castable_info**: Get table metadata (row count, columns, size) for the use-case table
- **get_castable_columns**: Get column names, types, labels, formats for the use-case table
- **get_castable_data**: Fetch raw sample rows from the use-case table

#### Visualization
- **render_chart**: Render a chart as a PNG image (bar/line/area/pie/scatter) server-side so it displays inline in the chat — pair it with `query_table`

#### Ready models & decisions (real-time scoring)
- **list_models_and_decisions**: List the ready (published) models and decisions you can score against (MAS modules)
- **score_data**: Score a record against a ready model or decision in real time

#### Model building (AutoML)
- **list_ml_projects**: List AutoML pipeline automation projects
- **create_ml_project**: Build a new ML model with AutoML
- **run_ml_project**: Run (train) an AutoML project
- **get_ml_project_results**: Get an AutoML project's state, champion model, and leaderboard with fit statistics
- **delete_ml_project**: Delete an AutoML project

### Prompt Templates

- **debug_sas_log**: Analyze SAS log for errors with root-cause explanations
- **explore_dataset**: Generate data-profiling SAS code
- **data_quality_check**: Generate DQ assessment code
- **statistical_analysis**: Set up a statistical workflow with diagnostics
- **optimize_sas_code**: Review and optimize SAS code
- **explain_sas_code**: Block-by-block code explanation
- **sas_macro_builder**: Build production-quality SAS macros
- **generate_report**: Generate ODS/PROC REPORT code

## Use-Case Scoping

This server is designed to be pointed at one use case — a single dataset plus
its associated models/decisions — and become an expert on it. You do that with
environment variables, no code changes:

| Variable | Purpose |
|---|---|
| `USE_CASE_NAME` / `USE_CASE_DESCRIPTION` | Identify the use case (returned by `get_use_case`) |
| `ALLOWED_TABLES` | The dataset(s). The **first** entry is the *primary* table the data tools default to. Each entry: `table`, `caslib.table`, or `server.caslib.table` |
| `ALLOWED_MODELS` | Ready model IDs or names the agent may score against |
| `ALLOWED_DECISIONS` | Decision / MAS-module IDs or names the agent may score against |
| `DEFAULT_CAS_SERVER` | CAS server used when a table entry omits it (default `cas-shared-default`) |
| `DEFAULT_CASLIB` | Caslib used when a table entry omits it (default `Public`) |
| `SCOPE_ENFORCE` | `true` (default) blocks out-of-scope access; `false` only hides it from listings |

Entries are comma- or newline-separated and matched case-insensitively against both IDs and names. When a scope is active:

- the data tools (`get_castable_info`/`columns`/`data`, `query_table`) **default to the primary table** — the agent calls them with no table arguments;
- `get_use_case` tells the agent its scope deterministically, including the primary table's **columns**, so it's grounded without relying on the system prompt;
- `list_models_and_decisions` returns **only** the allowed models/decisions;
- `score_data` **refuses** out-of-scope modules when `SCOPE_ENFORCE=true`;
- `execute_sas_code` and `query_table` remain unrestricted (so the agent can still freely analyse its dataset).

With none of the `ALLOWED_*` variables set, the server has full access to the environment. This makes it easy to stand up many per-use-case assistants from one image — for example, in **SAS Retrieval Agent Manager**, register the container once as a **Container MCP Server** code template, then create one tool server per use case and set these variables on its Environment Variables tab.

## MCP Client Configuration

Example configurations are provided in the `examples/` folder. Below are quick-start snippets for common clients.

### VS Code / Cursor / Claude Code (`.vscode/mcp.json`)

**HTTP mode** (requires `uv run app` running separately):
```json
{
    "servers": {
        "sas-execution-mcp": {
            "url": "http://localhost:8134/mcp",
            "type": "http"
        }
    }
}
```

**Stdio mode** (starts the server on demand):
```json
{
    "servers": {
        "sas-execution-mcp": {
            "command": "uv",
            "args": ["run", "app-stdio"],
            "cwd": "${workspaceFolder}"
        }
    }
}
```

### Gemini CLI (`.gemini/settings.json`)

Gemini CLI only supports stdio mode. Add to your `~/.gemini/settings.json` or project-level `.gemini/settings.json`:

```json
{
    "mcpServers": {
        "sas-viya-mcp": {
            "command": "uv",
            "args": ["run", "app-stdio"],
            "cwd": "/path/to/sas-mcp-server",
            "timeout": 60000
        }
    }
}
```

> **Note:** The `timeout` field (in milliseconds) is important — SAS Viya API calls can take longer than the Gemini CLI default of 10 seconds. A value of `60000` (60s) is recommended. Set `cwd` to the absolute path of your `sas-mcp-server` checkout.

## Example

Execute SAS code through the MCP tool:
```sas
data work.students;
input Name $ Age Grade $;
datalines;
Alice 20 A
Bob 22 B
;
run;

proc print data=work.students;
run;
```
---

**For more details, configuration options, and deployment options, please refer to the **examples** folder and follow the instructions listed there.**

## Testing

The project includes two layers of tests: **unit tests** (fast, no credentials required) and **integration tests** (run against a real SAS Viya instance).

### Running Unit Tests

Unit tests verify tool schemas, request payloads, and internal logic without making any network calls:

```sh
./run_tests.sh
```

Or directly via pytest:

```sh
uv run python -m pytest -m "not integration" -v
```

### Running Integration Tests

Integration tests call every tool against a live Viya environment. They require credentials, which can be provided via CLI arguments or `.env`:

**Using `.env`** (set `VIYA_ENDPOINT`, `VIYA_USERNAME`, `VIYA_PASSWORD`):
```sh
./run_tests.sh --integration
```

**Using CLI arguments:**
```sh
./run_tests.sh --integration \
    --endpoint https://your-viya-server.com \
    --username youruser \
    --password yourpassword
```

**Integration tests only** (skip unit tests):
```sh
./run_tests.sh --integration-only
```

### Test Structure

| File | Description |
|---|---|
| `tests/test_tool_payloads.py` | Payload assertions for the 14 tools — verifies the tool set, URL paths, JSON body structure, query params, and headers |
| `tests/test_usecase.py` | Use-case scoping, auto-scope resolution, and guard/filter behavior |
| `tests/test_integration.py` | End-to-end workflow tests against a real Viya instance |
| `tests/test_tools.py` | Unit tests for HTTP helper functions (`_get_json`, `_post_json`, etc.) |
| `tests/test_viya_utils.py` | Unit tests for Viya compute session and job utilities |
| `tests/test_mcp_server.py` | Unit tests for MCP server and auth middleware |
| `tests/test_prompts.py` | Unit tests for prompt template rendering |
| `tests/test_config.py` | Unit tests for configuration loading |

## Contributing
Maintainers are accepting patches and contributions to this project. Please read [CONTRIBUTING.md](CONTRIBUTING.md) for details about submitting contributions to this project.

## License & Attribution

Except for the the contents of the /static folder, this project is licensed under the [Apache 2.0 License](LICENSE). Elements in the /static folder are owned by SAS and are not released under an open source license. SAS and all other SAS Institute Inc. product or service names are registered trademarks or trademarks of SAS Institute Inc. in the USA and other countries. ® indicates USA registration.

Separate commercial licenses for SAS software (e.g., SAS Viya) are not included and are required to use these capabilities with SAS software.

All third-party trademarks referenced belong to their respective owners and are only used here for identification and reference purposes, and not to imply any affiliation or endorsement by the trademark owners.

This project requires the usage of the following:

- Python, see the Python license [here](https://docs.python.org/3/license.html)
- FastMCP, under the Apache 2.0 License
- uvicorn, under the BSD 3-Clause
- starlette, under the BSD 3-Clause
- httpx, under the MIT license
