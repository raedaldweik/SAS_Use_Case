## Running with Docker
This repo provides a Dockerfile for the MCP server. The same image is what SAS Retrieval Agent Manager
hosts, and it is published to GitHub Container Registry by `.github/workflows/build-and-push.yml`:

```sh
docker pull ghcr.io/raedaldweik/sas-mcp-usecase:latest
```

### Building
Basic:
```sh
# From the repository root
docker build -t sas-mcp-usecase .
```

You can also pass in the expected host port at build time:
```sh
docker build --build-arg HOST_PORT=8500 -t sas-mcp-usecase:8500 .
```

The image installs the dependency set pinned in `uv.lock` (with hash verification), so rebuilding a
commit reproduces its image.

### Running
The container expects its settings as environment variables — an `.env` file is the easy way:

```sh
# From the repository root
docker run -d -p 8134:8134 --env-file .env --name sas-usecase sas-mcp-usecase
```

By default the container runs in **direct HTTP mode** (`MCP_MODE=http-direct`): it authenticates to
Viya with `VIYA_REFRESH_TOKEN` (or `VIYA_USERNAME`/`VIYA_PASSWORD`) and serves MCP at
`http://localhost:8134/mcp`, protected by `MCP_API_KEY` when set. Set `MCP_MODE=http` for per-user
browser OAuth or `MCP_MODE=stdio` for the stdio transport.

If you set a non-default port at build time, make sure you set that in your .env!
```sh
docker run -d -p 8500:8500 --env-file .env --name sas-usecase sas-mcp-usecase:8500

# In this case the .env should also have an entry for
HOST_PORT=8500
```

`GET /health` answers without credentials, for liveness probes.

---

Usage is the exact same as when it was run locally.
