"""
Vinverse MCP Server

Exposes project state, documents, and membership data as MCP tools that the
LLM Orchestrator (a separate service/repo) calls as an MCP client. This server
owns no database of its own — it's a thin, well-defined interface in front of
the Application API's internal endpoints, so the Orchestrator never has direct
database access and the Application API stays the single source of truth.

Run with:
    python server.py

Requires the `mcp` Python SDK. The @mcp.tool() decorator API has been stable
across recent versions; the transport argument to mcp.run() has changed
between SDK releases (stdio-only in early versions, sse/streamable-http added
later). If `mcp.run(transport="streamable-http")` errors on your installed
version, check `pip show mcp` and adjust — worst case, run with the default
stdio transport and have the Orchestrator spawn this process directly instead
of connecting over HTTP.
"""
import os
import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

APPLICATION_API_URL = os.getenv("APPLICATION_API_URL", "http://localhost:8000")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "dev-internal-key-change-me")

HEADERS = {"X-Internal-Key": INTERNAL_API_KEY}

mcp = FastMCP(
    "vinverse-mcp-server",
    host="0.0.0.0",
    port=int(os.getenv("MCP_PORT", "8001")),
)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    """Plain HTTP health check for the ALB — separate from the MCP protocol itself."""
    return PlainTextResponse("OK")


@mcp.tool()
def get_project_state(project_id: str) -> dict:
    """
    Fetch the current canonical Project State for a project: problem,
    objective, stakeholders, evidence, recommendations, etc., plus its
    version number. This is the machine-readable representation of the
    project the LLM Orchestrator should reason over.
    """
    resp = httpx.get(f"{APPLICATION_API_URL}/internal/projects/{project_id}/state", headers=HEADERS, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def list_project_documents(project_id: str) -> list:
    """
    List all documents uploaded to a project, including their extracted/pasted
    text content, so the orchestrator can ground its analysis in evidence
    rather than assumption.
    """
    resp = httpx.get(f"{APPLICATION_API_URL}/internal/projects/{project_id}/documents", headers=HEADERS, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def get_document(document_id: str) -> dict:
    """Fetch a single document's full content by ID."""
    resp = httpx.get(f"{APPLICATION_API_URL}/internal/documents/{document_id}", headers=HEADERS, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def list_project_members(project_id: str) -> list:
    """
    List the people on a project and their project-scoped roles (owner,
    manager, participant, fellow, reviewer, observer) — useful for the
    orchestrator's game-theory reasoning about who is actually involved in
    a decision.
    """
    resp = httpx.get(f"{APPLICATION_API_URL}/internal/projects/{project_id}/members", headers=HEADERS, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    # streamable-http lets this run as an independent, network-reachable
    # service, matching the microservices split. Falls back to stdio if your
    # installed SDK version doesn't support this transport name — see the
    # module docstring above.
    mcp.run(transport="streamable-http")
