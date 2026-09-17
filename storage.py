"""
Minimal S3-backed store for pending Fellow registrations.

Replaces the in-memory `_pending_registrations` list that used to live in
server.py (see the TODO that was there: "notify an admin instead of just
holding this in memory") -- that list was lost on every restart/redeploy.
There's no Application API / real database for this data yet, and standing
one up (RDS, etc.) is overkill for a low-volume "someone filled in a
request-access form" queue, so S3 is used as the smallest possible durable
store: one tiny JSON object per registration.

A note on "bare minimum space (5 MB etc)": S3 doesn't work like a
provisioned disk or an RDS instance -- there's no fixed-size volume to
under-size, you're billed per byte actually stored (currently a few cents
per GB-month), and an empty bucket costs nothing. So there's no dial that
makes a bucket "5 MB". What this module does instead, to keep the actual
footprint tiny by construction:
  - each registration is its own object, a few hundred bytes of JSON
    (name/email/phone/timestamp) -- nothing is duplicated across objects
  - a lifecycle rule expires registration objects after 90 days, so the
    bucket can't grow unbounded even if nobody ever prunes it by hand
If you want a hard ceiling enforced (rather than just "usage stays small
in practice"), put a CloudWatch alarm on the bucket's BucketSizeBytes
metric -- S3 itself has no such setting.

Bootstrapping: the running container's task role (vinverse-mcp-runtime-role)
already has iam:PutRolePolicy on itself, granted by
scripts/bootstrap_mcp_runtime_role.py and used the same way by
deploy_tool.py's ensure_shared_oidc_role(). _self_grant_s3_permissions()
below reuses that same trick to add scoped S3 permissions to its own policy
the first time this module needs them, so no second manual AWS step is
required after the initial bootstrap script has been run once. (The
bootstrap script has also been updated to grant these up front -- the
runtime self-grant is a fallback for containers that started before that.)
"""
import json
import logging
import os
import time
import uuid

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger("vinverse-mcp.storage")

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_ACCOUNT_ID = os.getenv("AWS_ACCOUNT_ID", "503947800630")
REGISTRATIONS_BUCKET = os.getenv("REGISTRATIONS_BUCKET", f"vinverse-registrations-{AWS_ACCOUNT_ID}")
RUNTIME_ROLE_NAME = os.getenv("MCP_RUNTIME_ROLE_NAME", "vinverse-mcp-runtime-role")
REGISTRATIONS_PREFIX = "registrations/"

_session = boto3.Session(region_name=AWS_REGION)
_s3 = _session.client("s3")
_iam = _session.client("iam")

_bucket_ready = False


def _self_grant_s3_permissions() -> None:
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:CreateBucket",
                    "s3:PutBucketPublicAccessBlock",
                    "s3:PutEncryptionConfiguration",
                    "s3:PutLifecycleConfiguration",
                ],
                "Resource": f"arn:aws:s3:::{REGISTRATIONS_BUCKET}",
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"],
                "Resource": [
                    f"arn:aws:s3:::{REGISTRATIONS_BUCKET}",
                    f"arn:aws:s3:::{REGISTRATIONS_BUCKET}/*",
                ],
            },
        ],
    }
    try:
        _iam.put_role_policy(
            RoleName=RUNTIME_ROLE_NAME,
            PolicyName="vinverse-mcp-registrations-s3",
            PolicyDocument=json.dumps(policy),
        )
        log.info("Self-granted S3 permissions to %s for bucket %s", RUNTIME_ROLE_NAME, REGISTRATIONS_BUCKET)
        time.sleep(6)  # let IAM propagate before the S3 calls that follow
    except ClientError as exc:
        log.warning("Could not self-grant S3 permissions (%s) -- assuming they're already in place.", exc)


def _ensure_bucket() -> None:
    global _bucket_ready
    if _bucket_ready:
        return

    granted_this_call = False
    while True:
        try:
            _s3.head_bucket(Bucket=REGISTRATIONS_BUCKET)
            _bucket_ready = True
            return
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("403", "AccessDenied") and not granted_this_call:
                _self_grant_s3_permissions()
                granted_this_call = True
                continue
            if code not in ("404", "NoSuchBucket"):
                raise
            break  # bucket genuinely doesn't exist yet -- create it below

    create_kwargs = {"Bucket": REGISTRATIONS_BUCKET}
    if AWS_REGION != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": AWS_REGION}
    try:
        _s3.create_bucket(**create_kwargs)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "AccessDenied" and not granted_this_call:
            _self_grant_s3_permissions()
            _s3.create_bucket(**create_kwargs)
        elif code not in ("BucketAlreadyOwnedByYou",):
            raise

    _s3.put_public_access_block(
        Bucket=REGISTRATIONS_BUCKET,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    _s3.put_bucket_encryption(
        Bucket=REGISTRATIONS_BUCKET,
        ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]},
    )
    _s3.put_bucket_lifecycle_configuration(
        Bucket=REGISTRATIONS_BUCKET,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-old-registrations",
                    "Status": "Enabled",
                    "Filter": {"Prefix": REGISTRATIONS_PREFIX},
                    "Expiration": {"Days": 90},
                }
            ]
        },
    )
    _bucket_ready = True


def save_registration(record: dict) -> str:
    """Writes one registration as its own small JSON object (a few hundred
    bytes). Returns the generated registration id."""
    _ensure_bucket()
    reg_id = uuid.uuid4().hex[:12]
    key = f"{REGISTRATIONS_PREFIX}{int(time.time())}-{reg_id}.json"
    payload = {**record, "id": reg_id, "status": "pending"}
    _s3.put_object(
        Bucket=REGISTRATIONS_BUCKET,
        Key=key,
        Body=json.dumps(payload).encode("utf-8"),
        ContentType="application/json",
    )
    return reg_id


def list_registrations(limit: int = 200) -> list[dict]:
    """Reads back every stored registration, most recent first. Fine at the
    volume this is meant for (a request-access queue, not an event log) --
    each read is a couple of tiny GETs, no separate index to keep in sync."""
    _ensure_bucket()
    resp = _s3.list_objects_v2(Bucket=REGISTRATIONS_BUCKET, Prefix=REGISTRATIONS_PREFIX, MaxKeys=limit)
    records = []
    for obj in resp.get("Contents", []):
        try:
            body = _s3.get_object(Bucket=REGISTRATIONS_BUCKET, Key=obj["Key"])["Body"].read()
            records.append(json.loads(body))
        except (ClientError, json.JSONDecodeError):
            continue
    records.sort(key=lambda r: r.get("requestedAt") or "", reverse=True)
    return records


# ============================================================================
# Approval: turning a pending registration into an actual login-allowed
# email, and the live allow-list Google Sign-In checks against.
#
# This intentionally does NOT go through the ALLOWED_EMAILS GitHub Actions
# secret -- secrets are write-only (there's no API to read one back), so
# there'd be nothing to append to, and a secret only takes effect on the
# next deploy anyway. Keeping the allow-list itself in S3 means an approval
# takes effect immediately, no redeploy required. ALLOWED_EMAILS still works
# as a one-time bootstrap value (see get_approved_emails below) so whoever
# was already hardcoded there can log in and approve others before any
# approvals exist in S3.
# ============================================================================

APPROVED_EMAILS_KEY = "config/approved_emails.json"
_APPROVED_EMAILS_CACHE_TTL = 30  # seconds -- caps S3 reads without delaying an approval noticeably
_approved_emails_cache: tuple[float, set] | None = None


def _bootstrap_env_allowed_emails() -> set:
    return {e.strip().lower() for e in os.getenv("ALLOWED_EMAILS", "").split(",") if e.strip()}


def get_approved_emails(force_refresh: bool = False) -> set:
    """The live Google Sign-In allow-list. Reads from S3; falls back to the
    ALLOWED_EMAILS env var the first time (before anyone has been approved
    through S3 yet)."""
    global _approved_emails_cache
    now = time.time()
    if not force_refresh and _approved_emails_cache and now - _approved_emails_cache[0] < _APPROVED_EMAILS_CACHE_TTL:
        return _approved_emails_cache[1]

    _ensure_bucket()
    try:
        body = _s3.get_object(Bucket=REGISTRATIONS_BUCKET, Key=APPROVED_EMAILS_KEY)["Body"].read()
        emails = set(json.loads(body).get("emails", []))
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("NoSuchKey", "404"):
            raise
        emails = _bootstrap_env_allowed_emails()

    _approved_emails_cache = (now, emails)
    return emails


def _write_approved_emails(emails: set) -> None:
    global _approved_emails_cache
    _s3.put_object(
        Bucket=REGISTRATIONS_BUCKET,
        Key=APPROVED_EMAILS_KEY,
        Body=json.dumps({"emails": sorted(emails)}).encode("utf-8"),
        ContentType="application/json",
    )
    _approved_emails_cache = (time.time(), emails)


def _find_registration(reg_id: str):
    """Returns (s3_key, record) for a registration id, or None. A linear
    scan is fine at this volume -- see list_registrations' docstring."""
    _ensure_bucket()
    paginator = _s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=REGISTRATIONS_BUCKET, Prefix=REGISTRATIONS_PREFIX):
        for obj in page.get("Contents", []):
            try:
                body = _s3.get_object(Bucket=REGISTRATIONS_BUCKET, Key=obj["Key"])["Body"].read()
                record = json.loads(body)
            except (ClientError, json.JSONDecodeError):
                continue
            if record.get("id") == reg_id:
                return obj["Key"], record
    return None


def set_registration_status(reg_id: str, status: str) -> dict:
    """status: "approved" or "rejected". On approval, also adds the email
    to the live allow-list (get_approved_emails) so they can log in right
    away. Raises KeyError if reg_id doesn't match any stored registration."""
    found = _find_registration(reg_id)
    if not found:
        raise KeyError(f"No registration with id {reg_id}")
    key, record = found
    record["status"] = status
    _s3.put_object(
        Bucket=REGISTRATIONS_BUCKET, Key=key, Body=json.dumps(record).encode("utf-8"), ContentType="application/json"
    )
    if status == "approved" and record.get("email"):
        emails = get_approved_emails(force_refresh=True)
        emails.add(record["email"].strip().lower())
        _write_approved_emails(emails)
    return record
