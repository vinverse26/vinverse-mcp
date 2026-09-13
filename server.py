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
import time

import httpx
import jwt
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from deploy_tool import register_deploy_tools

APPLICATION_API_URL = os.getenv("APPLICATION_API_URL", "http://localhost:8000")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "dev-internal-key-change-me")

HEADERS = {"X-Internal-Key": INTERNAL_API_KEY}

# --- Google Sign-In config. No client secret needed -- verifying an ID
#     token only needs the client ID it was issued for (the "audience"). ---
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
SESSION_SECRET = os.environ["SESSION_SECRET"]
ALLOWED_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ALLOWED_EMAILS", "").split(",")
    if e.strip()
}
FRONTEND_ORIGINS = {
    o.strip()
    for o in os.environ.get(
        "FRONTEND_ORIGINS",
        "https://vinverse.ai,https://www.vinverse.ai,http://localhost:5173",
    ).split(",")
    if o.strip()
}
SESSION_COOKIE_NAME = "vinverse_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days
_google_request = google_requests.Request()
_pending_registrations: list[dict] = []  # in-memory placeholder, no DB yet

mcp = FastMCP(
    "vinverse-mcp-server",
    host="0.0.0.0",
    port=int(os.getenv("MCP_PORT", "8001")),
)

# Adds the deploy_app / deploy_app_status tools (see deploy_tool.py) --
# provisions the AWS pipeline for a new repo and deploys it, the same way
# this repo deploys itself.
register_deploy_tools(mcp)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    """Plain HTTP health check for the ALB — separate from the MCP protocol itself."""
    return PlainTextResponse("OK")


# ============================================================================
# Google Sign-In endpoints, called directly by vinverse-ui (src/api/auth.js).
# These are plain REST routes riding alongside the MCP protocol on the same
# Starlette app -- unrelated to the MCP tools below, which stay on the
# INTERNAL_API_KEY / APPLICATION_API_URL machine-to-machine path.
# CORS is handled by hand here (allow-listing FRONTEND_ORIGINS) rather than
# via Starlette's CORSMiddleware, since FastMCP's high-level run() doesn't
# give an easy hook to attach ASGI middleware and that surface has already
# proven to shift across mcp SDK versions (see README).
# ============================================================================


def _cors_headers(request: Request) -> dict:
    origin = request.headers.get("origin", "")
    headers = {"Vary": "Origin"}
    if origin in FRONTEND_ORIGINS:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
    return headers


def _preflight(request: Request) -> Response:
    headers = _cors_headers(request)
    headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    headers["Access-Control-Allow-Headers"] = "Content-Type"
    return Response(status_code=204, headers=headers)


def _json(request: Request, payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=_cors_headers(request))


async def _safe_json_body(request: Request) -> dict | None:
    """Returns the parsed JSON body, or None if it's missing/malformed —
    lets callers return a clean 400 instead of an unhandled 500."""
    try:
        return await request.json()
    except Exception:
        return None


@mcp.custom_route("/api/auth/register", methods=["POST", "OPTIONS"])
async def register_fellow(request: Request):
    if request.method == "OPTIONS":
        return _preflight(request)

    body = await _safe_json_body(request)
    if body is None:
        return _json(request, {"detail": "Invalid request body"}, 400)
    email = (body.get("email") or "").strip().lower()
    if not email:
        return _json(request, {"detail": "Email is required"}, 400)

    _pending_registrations.append(
        {
            "name": body.get("name", ""),
            "email": email,
            "phone": body.get("phone", ""),
            "requestedAt": body.get("requestedAt"),
        }
    )
    # TODO: notify an admin instead of just holding this in memory.
    return _json(request, {"status": "pending", "message": "Request recorded. Admin approval required."})


@mcp.custom_route("/api/auth/google", methods=["POST", "OPTIONS"])
async def google_login(request: Request):
    if request.method == "OPTIONS":
        return _preflight(request)

    body = await _safe_json_body(request)
    if body is None:
        return _json(request, {"detail": "Invalid request body"}, 400)
    credential = body.get("credential")
    if not credential:
        return _json(request, {"detail": "Missing credential"}, 400)

    try:
        claims = google_id_token.verify_oauth2_token(credential, _google_request, GOOGLE_CLIENT_ID)
    except ValueError:
        return _json(request, {"detail": "Invalid Google token"}, 401)

    email = (claims.get("email") or "").lower()
    if not claims.get("email_verified", False):
        return _json(request, {"detail": "Email not verified with Google"}, 401)

    if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
        # Valid Google account, but not an approved Fellow.
        return _json(
            request,
            {"detail": "You are not an authorized user. Please register to request access."},
            403,
        )

    name = claims.get("name", "")
    picture = claims.get("picture", "")
    session_payload = {
        "email": email,
        "name": name,
        "picture": picture,
        "iat": int(time.time()),
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
    token = jwt.encode(session_payload, SESSION_SECRET, algorithm="HS256")

    response = _json(
        request,
        {
            "token": "session-cookie",
            "user": {
                "name": name,
                "email": email,
                "picture": picture,
                "provider": "google",
                "role": "intelligence_fellow",
            },
        },
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )
    return response


@mcp.custom_route("/api/auth/session", methods=["GET", "OPTIONS"])
async def session(request: Request):
    if request.method == "OPTIONS":
        return _preflight(request)

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return _json(request, {"detail": "Not authenticated"}, 401)
    try:
        claims = jwt.decode(token, SESSION_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return _json(request, {"detail": "Session expired or invalid"}, 401)

    return _json(
        request,
        {
            "user": {
                "name": claims.get("name", ""),
                "email": claims["email"],
                "picture": claims.get("picture", ""),
                "provider": "google",
                "role": "intelligence_fellow",
            }
        },
    )


@mcp.custom_route("/api/auth/logout", methods=["POST", "OPTIONS"])
async def logout(request: Request):
    if request.method == "OPTIONS":
        return _preflight(request)

    response = _json(request, {"ok": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


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
