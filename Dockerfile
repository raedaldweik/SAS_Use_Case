FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base-builder

WORKDIR /app
COPY . .
# Build the wheel, and export the LOCKED dependency set alongside it. Without
# the export, the runner stage's `pip install <wheel>` would resolve
# dependencies fresh against the ranges in pyproject.toml and ignore uv.lock,
# so each rebuild would float to whatever is newest that day and a released
# tag would not reproduce the released image.
RUN uv build \
    && uv export --frozen --no-default-groups --no-emit-project \
        --format requirements.txt -o /app/dist/requirements.txt

FROM python:3.12-slim-bookworm AS runner
ARG HOST_PORT=8134

# Links the GHCR package to this repository (shown on the repo's Packages tab).
LABEL org.opencontainers.image.source=https://github.com/raedaldweik/SAS_Use_Case
LABEL org.opencontainers.image.description="SAS MCP Server — Use-Case Edition: query one dataset, score it against ready models in real time, chart the results"
LABEL org.opencontainers.image.licenses=Apache-2.0

RUN addgroup --system sas && adduser --system --ingroup sas --home /app sas

COPY --from=base-builder /app/dist/ /install

WORKDIR /app
# Dependencies from the lock first, then the project wheel with --no-deps so
# pip cannot re-resolve and undo the pinning. The export carries hashes, so pip
# verifies every artifact it downloads.
RUN python3 -m venv .venv \
    && /app/.venv/bin/pip install --no-cache-dir --require-hashes -r /install/requirements.txt \
    && /app/.venv/bin/pip install --no-cache-dir --no-deps /install/*.whl \
    && rm -r /install

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH"

USER sas

EXPOSE ${HOST_PORT}
# Default to direct HTTP mode so the container is ready to be hosted by an MCP
# client such as SAS Retrieval Agent Manager. Override the mode with MCP_MODE
# (http-direct|http|stdio) or by passing an explicit command.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
