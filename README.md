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
- `deploy_app(repo_name, project_type, github_repo, ...)` — provisions and deploys a new repo (see "Deploy automation" below)
- `deploy_app_status(repo_name, project_type, github_repo)` — checks deploy progress and live URL

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

## Deploy automation: deploy_app / deploy_app_status

Two new MCP tools, added in `deploy_tool.py` and registered from
`server.py`, let you (or the Master Consultant) provision and deploy a
**new** repo the same way this one deploys itself:

```
deploy_app(
  repo_name="vinverse_quant",
  project_type="python",          # "python" or "java" (react goes via Amplify separately)
  github_repo="https://github.com/vinverse26/vinverse_quant"
)
```

What it automates, per call:
1. Creates (once) a **shared IAM role** that GitHub Actions from any
   `vinverse26` repo can assume via OIDC — reused across every future
   repo instead of hand-creating a role each time.
2. Creates the ECR repository for the app if it doesn't exist.
3. Commits a `Dockerfile` (only if the repo doesn't already have one)
   and `.github/workflows/deploy.yml` into the target repo via the
   GitHub API — the same shape as this repo's own workflow, just
   templated per `project_type`.
4. That commit is itself a push to `main`, so the first deploy kicks
   off immediately — no separate trigger step.

Check progress and get the live URL with:
```
deploy_app_status(repo_name="vinverse_quant", project_type="python", github_repo="https://github.com/vinverse26/vinverse_quant")
```
This reads the latest GitHub Actions run status, and once the ECS
Express service is up, resolves its ALB endpoint via boto3. Point a
CNAME at that endpoint in Cloudflare (`vinverse-quant.vinverse.ai ->
<endpoint>`) — this one step stays manual since DNS for vinverse.ai
lives in Cloudflare, not Route53.

Calling `deploy_app` again for the same repo is safe — it updates the
existing role/workflow rather than duplicating anything.

### One-time setup before first use

1. **`GITHUB_TOKEN`** — a fine-grained GitHub PAT (Contents + Secrets:
   Read and write, scoped to the `vinverse26` org) needs to reach the
   running container as an env var. In production, store the real
   value as a repo secret named `DEPLOY_GITHUB_TOKEN` (already wired
   into `.github/workflows/deploy.yml` under the env name
   `GITHUB_TOKEN` — GitHub reserves the literal name `GITHUB_TOKEN`
   for its own use, so the secret itself is named differently).

2. **Runtime AWS permissions** — this is the one genuinely new piece.
   `deploy_tool.py` calls `iam`/`ecr`/`ecs`/`elbv2` *from inside the
   running MCP server container*, but the existing workflow only ever
   granted the ECS *execution* role (pull image, write logs) — nothing
   that lets your own code call AWS APIs. That's a separate *task
   role*. Run once, locally, with your existing AWS credentials:
   ```
   pip install boto3
   python scripts/bootstrap_mcp_runtime_role.py
   ```
   This creates `vinverse-mcp-runtime-role`, which `.github/workflows/deploy.yml`
   already references as `task-role-arn` — so the next push to `main`
   picks it up with no further edits.

3. Push to `main`. The next deploy of vinverse-mcp itself will have
   both `deploy_app` tools live and able to act on other repos.

### Security note

The shared OIDC role's permissions policy in `ensure_shared_oidc_role()`
grants fairly broad `ecs:*` / `elasticloadbalancing:*` — enough for the
Express Mode action to freely create/update services, target groups,
and ALBs per repo. Scope this down to the specific actions your fleet
actually needs once you've got a couple of real deployments under your
belt and know the pattern.

## A note on the MCP SDK version

The `@mcp.tool()` decorator API has been stable across recent `mcp` package
releases. The transport argument to `mcp.run()` has not — early versions only
supported stdio (spawned as a subprocess by the client), later versions added
`sse` and then `streamable-http` for a standalone network service like this
one is meant to be. If `mcp.run(transport="streamable-http")` errors on
whatever version `pip install` resolves for you, check `pip show mcp` and
adjust the call — worst case, fall back to stdio and have the Orchestrator
spawn this script directly as a subprocess instead of connecting over HTTP.
