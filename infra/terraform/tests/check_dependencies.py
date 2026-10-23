# LIFESPAN: permanent
"""Source check for ordering that `terraform test` cannot see.

A task reads its secrets once, when it starts, and talks to the database straight away, but nothing in the task definition points at the secret VALUES
or at the database instance (it only names secret ARNs), so Terraform would create the services first unless they say `depends_on`. The mocked provider in
guards.tftest.hcl does not model creation order, so this file reads ecs.tf and requires every service to list what it needs.

    python tests/check_dependencies.py        exit 0 when every required dependency is listed, 1 and the missing names otherwise
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STACK = os.path.dirname(HERE)

REQUIRED = {
    "web": [
        "aws_db_instance.relay",
        "aws_secretsmanager_secret_version.database_url",
        "aws_secretsmanager_secret_version.ingest_token",
        "aws_secretsmanager_secret_version.admin_token",
        "aws_secretsmanager_secret_version.relay_config",
        "aws_iam_role_policy.execution_web_secrets",
        "aws_iam_role_policy_attachment.execution_web_managed",
        "aws_lb_listener.http",
        "aws_lb_listener.https",
    ],
    "worker": [
        "aws_db_instance.relay",
        "aws_secretsmanager_secret_version.database_url",
        "aws_secretsmanager_secret_version.relay_config",
        "aws_iam_role_policy.execution_worker_secrets",
        "aws_iam_role_policy_attachment.execution_worker_managed",
    ],
}


def service_block(text, name):
    """The body of `resource "aws_ecs_service" "<name>" { ... }`, up to the next top level resource."""
    start = text.find('resource "aws_ecs_service" "%s" {' % name)
    if start < 0:
        return None
    nxt = text.find("\nresource ", start + 10)
    return text[start: nxt if nxt > 0 else len(text)]


def depends_on(block):
    m = re.search(r"depends_on\s*=\s*\[(.{0,2000}?)\]", block, re.S)
    if not m:
        return set()
    body = re.sub(r"#[^\n]{0,400}", "", m.group(1))
    return {x.strip() for x in body.split(",") if x.strip()}


def main():
    with open(os.path.join(STACK, "ecs.tf"), encoding="utf-8") as fh:
        text = fh.read()
    problems = []
    for name, needed in REQUIRED.items():
        block = service_block(text, name)
        if block is None:
            problems.append("service %s not found in ecs.tf" % name)
            continue
        have = depends_on(block)
        missing = [n for n in needed if n not in have]
        if missing:
            problems.append("service %s does not depend on: %s" % (name, ", ".join(missing)))
    for p in problems:
        print("FAIL " + p)
    print("DEPENDENCIES " + ("FAIL" if problems else "PASS (%d services checked)" % len(REQUIRED)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
