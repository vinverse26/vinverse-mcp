"""
Deploy automation tool for vinverse-mcp.

Exposes two new MCP tools alongside the existing project-data tools in
server.py:

  - deploy_app(repo_name, project_type, github_repo, branch, container_port, health_check_path)
  - deploy_status(repo_name, project_type, github_repo)

What deploy_app actually does, mirroring exactly what was done by hand
for vinverse-mcp itself (see .github/workflows/deploy.yml):

  react:
    Deployed via AWS Amplify Hosting (same as vinverse-ui) -- Amplify
    manages its own build/deploy once connected to the GitHub repo, so
    there's no workflow file to generate for this path.

  python / java:
    1. Ensure a shared IAM role exists that GitHub Actions (any repo
       under GITHUB_ORG) can assume via OIDC -- created once, reused
       by every repo instead of a role per repo.
    2. Ensure an ECR repository exists for the app.
    3. Commit a Dockerfile (only if the repo doesn't already have one)
       and a .github/workflows/deploy.yml into the target repo via the
       GitHub API -- templated per project_type, using
       aws-actions/amazon-ecs-deploy-express-service, identical in
       shape to vinverse-mcp's own workflow.
    4. That commit IS a push to main, so the workflow fires
       immediately -- no separate "trigger" step needed.

deploy_status polls the latest GitHub Actions run for that repo and,
once the ECS Express service exists, resolves its ALB endpoint via
boto3 so you get back a real URL to point DNS at.

IMPORTANT -- this module makes AWS API calls (iam, ecr, ecs, elbv2) at
runtime, from inside the running vinverse-mcp container. That means
the ECS task needs its own AWS permissions (a *task role*, separate
from the *execution role* used just to pull the image / write logs).
See README.md's "Runtime AWS permissions" section for the one-time
setup this requires -- the deploy.yml addition to wire a task role in,
and the bootstrap script to create it.
"""
import base64
import json
import logging
import os
import time

import boto3
import httpx
from nacl import encoding, public

log = logging.getLogger("vinverse-mcp.deploy")

# --- config, same style as the rest of server.py: read from env, sane defaults ---
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_ACCOUNT_ID = os.getenv("AWS_ACCOUNT_ID", "503947800630")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_ORG = os.getenv("GITHUB_ORG", "vinverse26")
ROOT_DOMAIN = os.getenv("ROOT_DOMAIN", "vinverse.ai")

ECS_CLUSTER = os.getenv("ECS_CLUSTER", "default")
ECS_EXECUTION_ROLE_ARN = os.getenv(
    "ECS_EXECUTION_ROLE_ARN", f"arn:aws:iam::{AWS_ACCOUNT_ID}:role/service-role/ecsTaskExecutionRole"
)
ECS_INFRASTRUCTURE_ROLE_ARN = os.getenv(
    "ECS_INFRASTRUCTURE_ROLE_ARN",
    f"arn:aws:iam::{AWS_ACCOUNT_ID}:role/service-role/ecsInfrastructureRoleForExpressServices",
)
GITHUB_OIDC_ROLE_NAME = os.getenv("GITHUB_OIDC_ROLE_NAME", "github-actions-vinverse-org-deploy")

GITHUB_API = "https://api.github.com"
_gh_headers = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

_session = boto3.Session(region_name=AWS_REGION)
_iam = _session.client("iam")
_ecr = _session.client("ecr")
_ecs = _session.client("ecs")
_elbv2 = _session.client("elbv2")

FALLBACK_DOCKERFILES = {
    "python": """\
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
""",
    "java": """\
FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /app
COPY . .
RUN mvn -q -DskipTests package

FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /app/target/*.jar app.jar
EXPOSE 8080
CMD ["java", "-jar", "app.jar"]
""",
}
DEFAULT_PORTS = {"python": 8000, "java": 8080}


def dns_safe(name: str) -> str:
    return name.lower().replace("_", "-")


def _split_owner_repo(github_repo: str) -> tuple[str, str]:
    parts = github_repo.rstrip("/").split("/")
    return parts[-2], parts[-1]


# ============================================================================
# IAM: one shared role that GitHub Actions in ANY vinverse26 repo can assume
# ============================================================================

OIDC_PROVIDER_ARN = f"arn:aws:iam::{AWS_ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
# GitHub's OIDC thumbprint. AWS no longer strictly validates this (it
# validates against GitHub's actual TLS chain), but the API still requires
# a value be supplied.
_GITHUB_OIDC_THUMBPRINT = "6938fd4d98bab03faadb97b34396831e3780aea1"


def _ensure_github_oidc_provider() -> None:
    try:
        _iam.get_open_id_connect_provider(OpenIDConnectProviderArn=OIDC_PROVIDER_ARN)
    except _iam.exceptions.NoSuchEntityException:
        log.info("Creating GitHub OIDC provider (one-time, account-wide)")
        _iam.create_open_id_connect_provider(
            Url="https://token.actions.githubusercontent.com",
            ClientIDList=["sts.amazonaws.com"],
            ThumbprintList=[_GITHUB_OIDC_THUMBPRINT],
        )


def ensure_shared_oidc_role() -> str:
    """
    Idempotent: creates the shared role on first call, just refreshes its
    trust/permissions policy on every later call so deploy_app never fails
    with "already exists".
    """
    _ensure_github_oidc_provider()

    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Federated": OIDC_PROVIDER_ARN},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
                    # Any repo, any branch/PR/tag, under the org. Tighten to
                    # e.g. f"repo:{GITHUB_ORG}/*:ref:refs/heads/main" if you
                    # only want this usable from main-branch pushes.
                    "StringLike": {"token.actions.githubusercontent.com:sub": f"repo:{GITHUB_ORG}/*:*"},
                },
            }
        ],
    }

    try:
        _iam.get_role(RoleName=GITHUB_OIDC_ROLE_NAME)
        _iam.update_assume_role_policy(RoleName=GITHUB_OIDC_ROLE_NAME, PolicyDocument=json.dumps(trust_policy))
    except _iam.exceptions.NoSuchEntityException:
        log.info("Creating shared GitHub Actions deploy role: %s", GITHUB_OIDC_ROLE_NAME)
        _iam.create_role(RoleName=GITHUB_OIDC_ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust_policy))
        time.sleep(8)  # IAM propagation before the first workflow run tries to assume it

    # Permissions this role needs to build+push to ECR and let the Express
    # Mode action create/update its ECS service, ALB, and target group.
    permissions_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
            {
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                    "ecr:PutImage",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:CreateRepository",
                    "ecr:DescribeRepositories",
                ],
                "Resource": "*",
            },
            {"Effect": "Allow", "Action": "ecs:*", "Resource": "*"},
            {"Effect": "Allow", "Action": "elasticloadbalancing:*", "Resource": "*"},
            {
                "Effect": "Allow",
                "Action": ["ec2:DescribeSubnets", "ec2:DescribeVpcs", "ec2:DescribeSecurityGroups"],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups"],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": [ECS_EXECUTION_ROLE_ARN, ECS_INFRASTRUCTURE_ROLE_ARN],
            },
        ],
    }
    _iam.put_role_policy(
        RoleName=GITHUB_OIDC_ROLE_NAME, PolicyName="vinverse-express-deploy", PolicyDocument=json.dumps(permissions_policy)
    )

    return _iam.get_role(RoleName=GITHUB_OIDC_ROLE_NAME)["Role"]["Arn"]


# ============================================================================
# ECR
# ============================================================================


def ensure_ecr_repo(name: str) -> str:
    try:
        resp = _ecr.describe_repositories(repositoryNames=[name])
        return resp["repositories"][0]["repositoryUri"]
    except _ecr.exceptions.RepositoryNotFoundException:
        resp = _ecr.create_repository(repositoryName=name, imageScanningConfiguration={"scanOnPush": True})
        return resp["repository"]["repositoryUri"]


# ============================================================================
# GitHub API: commit files, read/write repo secrets, read workflow runs
# ============================================================================


def ensure_github_repo(owner: str, repo: str, branch: str = "main") -> bool:
    """
    Creates the GitHub repo if it doesn't exist yet, with auto_init=True so
    it gets an initial commit and a real `branch` to commit onto -- a
    brand-new empty repo has zero commits/branches, and the Contents API
    (used by github_put_file) 404s if the target branch doesn't exist.
    Returns True if the repo was just created, False if it already existed.
    """
    r = httpx.get(f"{GITHUB_API}/repos/{owner}/{repo}", headers=_gh_headers, timeout=15)
    if r.status_code == 200:
        return False
    if r.status_code != 404:
        r.raise_for_status()

    # Try creating under the org first (the common case here); fall back to
    # the authenticated user's own account if `owner` isn't an org the token
    # can create repos in.
    body = {"name": repo, "private": True, "auto_init": True}
    r = httpx.post(f"{GITHUB_API}/orgs/{owner}/repos", headers=_gh_headers, json=body, timeout=20)
    if r.status_code >= 400:
        r = httpx.post(f"{GITHUB_API}/user/repos", headers=_gh_headers, json=body, timeout=20)
    r.raise_for_status()

    # auto_init creates the repo's *default* branch, which may not be named
    # "main" depending on the account/org's settings -- if our target branch
    # is different, create it pointing at the same initial commit.
    default_branch = r.json().get("default_branch", "main")
    if default_branch != branch:
        ref = httpx.get(f"{GITHUB_API}/repos/{owner}/{repo}/git/ref/heads/{default_branch}", headers=_gh_headers, timeout=15)
        ref.raise_for_status()
        sha = ref.json()["object"]["sha"]
        httpx.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/refs",
            headers=_gh_headers,
            json={"ref": f"refs/heads/{branch}", "sha": sha},
            timeout=15,
        )
    return True


def _github_get_file_sha(owner: str, repo: str, path: str, branch: str) -> str | None:
    r = httpx.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}", headers=_gh_headers, params={"ref": branch}, timeout=15
    )
    if r.status_code == 200:
        return r.json()["sha"]
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return None


def github_put_file(owner: str, repo: str, path: str, content: str, message: str, branch: str = "main") -> dict:
    sha = _github_get_file_sha(owner, repo, path, branch)
    body = {"message": message, "content": base64.b64encode(content.encode()).decode(), "branch": branch}
    if sha:
        body["sha"] = sha
    r = httpx.put(f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}", headers=_gh_headers, json=body, timeout=20)
    r.raise_for_status()
    return r.json()


def github_set_secret(owner: str, repo: str, secret_name: str, secret_value: str) -> None:
    r = httpx.get(f"{GITHUB_API}/repos/{owner}/{repo}/actions/secrets/public-key", headers=_gh_headers, timeout=15)
    r.raise_for_status()
    key_info = r.json()

    public_key = public.PublicKey(key_info["key"].encode(), encoding.Base64Encoder())
    encrypted = public.SealedBox(public_key).encrypt(secret_value.encode())

    body = {"encrypted_value": base64.b64encode(encrypted).decode(), "key_id": key_info["key_id"]}
    r = httpx.put(
        f"{GITHUB_API}/repos/{owner}/{repo}/actions/secrets/{secret_name}", headers=_gh_headers, json=body, timeout=15
    )
    r.raise_for_status()


def github_latest_workflow_run(owner: str, repo: str, workflow_file: str = "deploy.yml") -> dict | None:
    r = httpx.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs",
        headers=_gh_headers,
        params={"per_page": 1},
        timeout=15,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    runs = r.json().get("workflow_runs", [])
    return runs[0] if runs else None


# ============================================================================
# Workflow template -- identical shape to vinverse-mcp's own deploy.yml
# ============================================================================


def _build_workflow_yaml(app_name: str, ecr_repo_name: str, container_port: int, oidc_role_arn: str, health_check_path: str) -> str:
    service_name = f"{app_name}-svc"
    return f"""name: Deploy to ECS Express Mode

on:
  push:
    branches:
      - main

permissions:
  id-token: write
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v6

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v5
        with:
          role-to-assume: {oidc_role_arn}
          aws-region: {AWS_REGION}

      - name: Login to Amazon ECR
        id: login-ecr
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build and push image
        env:
          ECR_REGISTRY: ${{{{ steps.login-ecr.outputs.registry }}}}
          ECR_REPOSITORY: {ecr_repo_name}
          IMAGE_TAG: ${{{{ github.sha }}}}
        run: |
          docker build -t $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG .
          docker push $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG
          docker tag $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG $ECR_REGISTRY/$ECR_REPOSITORY:latest
          docker push $ECR_REGISTRY/$ECR_REPOSITORY:latest

      - name: Deploy to ECS Express Mode
        uses: aws-actions/amazon-ecs-deploy-express-service@v1
        with:
          service-name: {service_name}
          image: ${{{{ steps.login-ecr.outputs.registry }}}}/{ecr_repo_name}:${{{{ github.sha }}}}
          execution-role-arn: {ECS_EXECUTION_ROLE_ARN}
          infrastructure-role-arn: {ECS_INFRASTRUCTURE_ROLE_ARN}
          cluster: {ECS_CLUSTER}
          container-port: {container_port}
          health-check-path: {health_check_path}
"""


# ============================================================================
# ECS: resolve a running Express service's live endpoint
# ============================================================================


def get_service_endpoint(service_name: str) -> str | None:
    try:
        resp = _ecs.describe_services(cluster=ECS_CLUSTER, services=[service_name])
    except Exception:  # noqa: BLE001
        return None

    services = [s for s in resp.get("services", []) if s["status"] == "ACTIVE"]
    if not services or not services[0].get("loadBalancers"):
        return None

    tg_arn = services[0]["loadBalancers"][0]["targetGroupArn"]
    tg = _elbv2.describe_target_groups(TargetGroupArns=[tg_arn])["TargetGroups"][0]
    lb = _elbv2.describe_load_balancers(LoadBalancerArns=[tg["LoadBalancerArns"][0]])["LoadBalancers"][0]
    return lb["DNSName"]


# ============================================================================
# Public entry points, called by the MCP tools below
# ============================================================================


def deploy_backend(
    repo_name: str,
    project_type: str,
    github_repo: str,
    branch: str = "main",
    container_port: int | None = None,
    health_check_path: str = "/",
) -> dict:
    owner, repo = _split_owner_repo(github_repo)
    app_name = dns_safe(repo_name)
    port = container_port or DEFAULT_PORTS[project_type]

    repo_created = ensure_github_repo(owner, repo, branch)
    oidc_role_arn = ensure_shared_oidc_role()
    ecr_uri = ensure_ecr_repo(app_name)

    if repo_created or _github_get_file_sha(owner, repo, "Dockerfile", branch) is None:
        github_put_file(
            owner, repo, "Dockerfile", FALLBACK_DOCKERFILES[project_type],
            "Add Dockerfile (auto-generated by deploy_app)", branch,
        )

    workflow_yaml = _build_workflow_yaml(app_name, app_name, port, oidc_role_arn, health_check_path)
    github_put_file(
        owner, repo, ".github/workflows/deploy.yml", workflow_yaml,
        "Add/update ECS Express deploy workflow (auto-generated by deploy_app)", branch,
    )

    return {
        "status": "triggered",
        "github_repo_created": repo_created,
        "ecr_repo_uri": ecr_uri,
        "service_name": f"{app_name}-svc",
        "workflow_runs_url": f"https://github.com/{owner}/{repo}/actions",
        "note": "Committing the workflow file just triggered the first run. Call deploy_status to check progress and get the live endpoint once it's up.",
    }


def deploy_status(repo_name: str, project_type: str, github_repo: str | None = None) -> dict:
    app_name = dns_safe(repo_name)
    service_name = f"{app_name}-svc"
    result: dict = {"repo_name": repo_name, "service_name": service_name}

    if github_repo:
        owner, repo = _split_owner_repo(github_repo)
        run = github_latest_workflow_run(owner, repo)
        if run:
            result["workflow_status"] = run["status"]  # queued | in_progress | completed
            result["workflow_conclusion"] = run.get("conclusion")  # success | failure | None
            result["workflow_run_url"] = run["html_url"]
        else:
            result["workflow_status"] = "not_found"

    endpoint = get_service_endpoint(service_name)
    if endpoint:
        result["endpoint"] = f"http://{endpoint}"
        result["dns_instructions"] = f"Add a CNAME in Cloudflare: {app_name}.{ROOT_DOMAIN} -> {endpoint}"
    else:
        result["endpoint"] = None

    return result


def register_deploy_routes(mcp) -> None:
    """
    Plain REST wrappers around deploy_app / deploy_app_status, so you can
    curl/Postman these instead of speaking the MCP protocol -- useful for
    quick testing, or for triggering a deploy from somewhere that isn't
    an MCP client at all.

    These sit alongside the MCP tools the same way /api/auth/* sits
    alongside the project-data tools in server.py: same process, same
    Starlette app, different protocol on the wire.

    Protected by a shared header (X-Deploy-Key / DEPLOY_API_KEY) since
    this can trigger real AWS/GitHub changes -- unlike /health, this
    should never be left open on a public URL.
    """
    import os as _os

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    DEPLOY_API_KEY = _os.getenv("DEPLOY_API_KEY", "")

    def _check_key(request: Request) -> bool:
        if not DEPLOY_API_KEY:
            return False  # refuse to run wide open if nobody set a key
        return request.headers.get("X-Deploy-Key") == DEPLOY_API_KEY

    @mcp.custom_route("/api/deploy", methods=["POST"])
    async def deploy_via_rest(request: Request):
        if not _check_key(request):
            return JSONResponse({"detail": "Missing or invalid X-Deploy-Key"}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "Invalid JSON body"}, status_code=400)

        repo_name = body.get("repo_name")
        project_type = body.get("project_type")
        github_repo = body.get("github_repo")
        if not all([repo_name, project_type, github_repo]):
            return JSONResponse({"detail": "repo_name, project_type, and github_repo are required"}, status_code=400)

        if project_type not in ("python", "java"):
            return JSONResponse({"detail": f"Unsupported project_type '{project_type}' for this route -- react apps deploy via Amplify instead."}, status_code=400)

        try:
            result = deploy_backend(
                repo_name,
                project_type,
                github_repo,
                body.get("branch", "main"),
                body.get("container_port"),
                body.get("health_check_path", "/"),
            )
            return JSONResponse(result)
        except Exception as exc:  # noqa: BLE001
            log.exception("deploy_via_rest failed for %s", repo_name)
            return JSONResponse({"repo_name": repo_name, "status": "error", "error": str(exc)}, status_code=500)

    @mcp.custom_route("/api/deploy/status", methods=["GET"])
    async def deploy_status_via_rest(request: Request):
        if not _check_key(request):
            return JSONResponse({"detail": "Missing or invalid X-Deploy-Key"}, status_code=401)

        repo_name = request.query_params.get("repo_name")
        project_type = request.query_params.get("project_type")
        github_repo = request.query_params.get("github_repo")
        if not repo_name or not project_type:
            return JSONResponse({"detail": "repo_name and project_type query params are required"}, status_code=400)

        return JSONResponse(deploy_status(repo_name, project_type, github_repo))


def register_deploy_tools(mcp) -> None:
    """Call this once from server.py: register_deploy_tools(mcp)"""

    @mcp.tool()
    def deploy_app(
        repo_name: str,
        project_type: str,
        github_repo: str,
        branch: str = "main",
        container_port: int | None = None,
        health_check_path: str = "/",
    ) -> dict:
        """
        Provision the AWS pipeline for a repo and deploy it, the same way
        vinverse-mcp itself is deployed. Creates the GitHub repo itself
        (private, with an initial commit) if it doesn't exist yet -- you
        don't need to create it by hand first.

        Args:
            repo_name: short app name, e.g. "vinverse_quant". Used as the
                ECR repo name, ECS service name prefix, and subdomain.
            project_type: "python" or "java". (React apps go through
                Amplify separately, not this path.)
            github_repo: full URL, e.g. https://github.com/vinverse26/vinverse_quant
                -- the repo is created here if it doesn't already exist.
            branch: branch the workflow triggers on (default "main").
            container_port: port the app listens on inside the container.
                Defaults to 8000 for python, 8080 for java.
            health_check_path: path the ALB health-checks (default "/").

        Idempotent: safe to call again after pushing new commits or
        changing settings -- updates the existing role/repo/workflow
        instead of duplicating anything.
        """
        try:
            if project_type not in ("python", "java"):
                return {"status": "error", "error": f"Unsupported project_type '{project_type}' for this tool -- react apps deploy via Amplify instead."}
            return deploy_backend(repo_name, project_type, github_repo, branch, container_port, health_check_path)
        except Exception as exc:  # noqa: BLE001
            log.exception("deploy_app failed for %s", repo_name)
            return {"repo_name": repo_name, "status": "error", "error": str(exc)}

    @mcp.tool()
    def deploy_app_status(repo_name: str, project_type: str, github_repo: str | None = None) -> dict:
        """
        Check progress for a repo previously deployed with deploy_app:
        the latest GitHub Actions run status, and the live ALB endpoint
        once the ECS Express service is up.
        """
        return deploy_status(repo_name, project_type, github_repo)
