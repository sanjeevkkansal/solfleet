# Container image for the solfleet MCP server (stdio transport).
# Used by Glama for introspection checks, and to run the server in a sandbox.
# The server starts with no fleet.yaml; config is loaded per tool call, so
# `initialize` and `tools/list` work in a bare container.
FROM python:3.12-slim

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .

# stdio MCP server: clients (and Glama) talk to it over stdin/stdout.
ENTRYPOINT ["solfleet-mcp"]
