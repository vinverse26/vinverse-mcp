"""
Run this ONCE, locally, with your existing "architect" IAM user
credentials (the same ones used to set up Amplify/vinverse-ui) --
before deploy_app is called for the first time.

It creates the IAM role that gets passed to the ECS Express action as
`task-role-arn` in .github/workflows/deploy.yml. Without this,
deploy_tool.py's boto3 calls (iam, ecr, ecs, elbv2) fail with
AccessDenied the moment they run inside the deployed container --
the *execution* role only grants ECS itself permission to pull the
image and write logs, it doesn't hand any AWS permissions to your
application code. A *task* role is what does that, and this is it.

Usage:
    pip install boto3
    python scripts/bootstrap_mcp_runtime_role.py

Then push to main -- deploy.yml already references
arn:aws:iam::503947800630:role/vinverse-mcp-runtime-role as
task-role-arn, so nothing else needs editing once this exists.
"""
import json
import os
import time

import boto3

ROLE_NAME = "vinverse-mcp-runtime-role"
REGION = "us-east-1"
AWS_ACCOUNT_ID = os.getenv("AWS_ACCOUNT_ID", "503947800630")
# Must match storage.py's REGISTRATIONS_BUCKET default.
REGISTRATIONS_BUCKET = os.getenv("REGISTRATIONS_BUCKET", f"vinverse-registrations-{AWS_ACCOUNT_ID}")

TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}],
}

# Everything deploy_tool.py needs to do at runtime: manage the shared
# GitHub OIDC role/provider, create ECR repos, and read back ECS/ELB
# state for deploy_app_status.
PERMISSIONS_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "iam:CreateOpenIDConnectProvider",
                "iam:GetOpenIDConnectProvider",
                "iam:CreateRole",
                "iam:GetRole",
                "iam:UpdateAssumeRolePolicy",
                "iam:PutRolePolicy",
            ],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": ["ecr:CreateRepository", "ecr:DescribeRepositories"],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": ["ecs:DescribeServices", "ecs:DescribeClusters"],
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": ["elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:DescribeLoadBalancers"],
            "Resource": "*",
        },
        # For storage.py's S3-backed registration queue (see /api/auth/register).
        # storage.py can also self-grant this at runtime (it reuses the
        # iam:PutRolePolicy permission above), but granting it here up front
        # avoids relying on that fallback.
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


def main():
    iam = boto3.client("iam", region_name=REGION)

    try:
        iam.get_role(RoleName=ROLE_NAME)
        print(f"Role {ROLE_NAME} already exists, updating its policy...")
    except iam.exceptions.NoSuchEntityException:
        print(f"Creating role {ROLE_NAME}...")
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(TRUST_POLICY))
        time.sleep(8)

    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="vinverse-mcp-runtime", PolicyDocument=json.dumps(PERMISSIONS_POLICY))

    role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
    print(f"\nDone. Role ARN: {role_arn}")
    print("This should already match task-role-arn in .github/workflows/deploy.yml.")
    print("Push to main (or re-run the workflow) to pick it up.")


if __name__ == "__main__":
    main()
