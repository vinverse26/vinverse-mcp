# Vinverse MCP Server

Exposes Vinverse project data (state, documents, membership) as MCP tools, so
the [LLM Orchestrator](../vinverse-llm) can fetch exactly the context it
needs via the Model Context Protocol instead of trusting whatever the caller
hands it.

The MCP tools below own no data themselves. Each one turns into an HTTP
request to an Application API's `/internal/*` endpoints, authenticated with
a shared secret (`INTERNAL_API_KEY`) rather than per-user JWTs — a
service-to-service call, not a user acting on their own behalf. That
Application API is a separate piece you still need to stand up (or point
`APPLICATION_API_URL` at once it exists).

## Also handles: Google Sign-In for vinverse-ui

This service additionally exposes plain REST endpoints
(`/api/auth/google`, `/api/auth/logout`, `/api/auth/session`,
`/api/auth/register`) that `vinverse-ui` calls directly for login. These
ride alongside the MCP protocol on the same Starlette app but are otherwise
unrelated to the tools below — they authenticate real users with a session
cookie, not the `INTERNAL_API_KEY` machine-to-machine path.

Worth knowing: this collapses two different trust boundaries (public
user-facing auth, and an internal service-to-service tool layer) into one
process. That's a pragmatic call for now — if this ever needs to scale or
be secured independently, auth is the piece that should move to its own
service first.

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
