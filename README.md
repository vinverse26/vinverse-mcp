# Vinverse MCP Server

Exposes Vinverse project data (state, documents, membership) as MCP tools, so
the [LLM Orchestrator](../vinverse-llm-orchestrator) can fetch exactly the
context it needs via the Model Context Protocol instead of trusting whatever
the caller hands it.

This service owns no data itself. Every tool call turns into an HTTP request
to the [Application API](../vinverse-platform/backend)'s `/internal/*`
endpoints, authenticated with a shared secret (`INTERNAL_API_KEY`) rather than
per-user JWTs — this is a service-to-service call, not a user acting on their
own behalf.

## Tools exposed

- `get_project_state(project_id)` — the canonical Project State (problem,
  objective, stakeholders, evidence, recommendations, etc.) plus version number
- `list_project_documents(project_id)` — all documents with their text content
- `get_document(document_id)` — a single document's full content
- `list_project_members(project_id)` — who's on the project and their role

## Running it

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: INTERNAL_API_KEY must match the Application API's value exactly,
# and APPLICATION_API_URL must point at wherever that service is running
python server.py
```

This must be run **alongside** the Application API (backend) — it has nothing
to serve on its own otherwise. Start the Application API first, confirm
`/health` responds, then start this.

## A note on the MCP SDK version

The `@mcp.tool()` decorator API has been stable across recent `mcp` package
releases. The transport argument to `mcp.run()` has not — early versions only
supported stdio (spawned as a subprocess by the client), later versions added
`sse` and then `streamable-http` for a standalone network service like this
one is meant to be. If `mcp.run(transport="streamable-http")` errors on
whatever version `pip install` resolves for you, check `pip show mcp` and
adjust the call — worst case, fall back to stdio and have the Orchestrator
spawn this script directly as a subprocess instead of connecting over HTTP.
