# LIFESPAN: permanent
"""Source check for the secret wiring that the mocked provider cannot tell apart on its own.

    python tests/check_wiring.py        exit 0 when every secret name points at the secret of that name in every place it appears

A task definition names a Secrets Manager entry by ARN, and an IAM policy lists the ARNs a role may read. A swap (the ingest token's entry pointing at the admin
secret, the worker policy listing a token it does not use) is valid Terraform and applies cleanly, so this reads ecs.tf and checks the whole table of
(service x secret name) instead of the few cases that happened to be found.
"""
import itertools
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STACK = os.path.dirname(HERE)

OWN = {  # environment variable name -> the resource that must back it
    "DATABASE_URL": "database_url",
    "RELAY_INGEST_TOKEN": "ingest_token",
    "RELAY_ADMIN_TOKEN": "admin_token",
    "RELAY_CONFIG": "relay_config",
}
SERVICES = {  # service -> (locals list name, policy document name, secrets it may hold)
    "web": ("secrets_web", "read_secrets_web", ["DATABASE_URL", "RELAY_INGEST_TOKEN", "RELAY_ADMIN_TOKEN", "RELAY_CONFIG"]),
    "worker": ("secrets_worker", "read_secrets_worker", ["DATABASE_URL", "RELAY_CONFIG"]),
}


def block(text, start_pattern, length=2500):
    m = re.search(start_pattern, text)
    return text[m.start(): m.start() + length] if m else ""


def main():
    with open(os.path.join(STACK, "ecs.tf"), encoding="utf-8") as fh:
        text = fh.read()
    problems = []
    # CLASS-TABLE: every (service x environment variable) pair, the vocabulary being the four secrets and the two services
    for service, name in itertools.product(SERVICES, OWN):
        locals_name, policy_name, allowed = SERVICES[service]
        in_list = block(text, r"%s\s*=\s*concat\(\[" % locals_name, 900).split("], local.vendor_secrets)")[0]
        entry = re.search(r'\{\s*name\s*=\s*"%s"\s*,\s*valueFrom\s*=\s*aws_secretsmanager_secret\.(\w+)\.arn\s*\}' % name, in_list)
        policy = block(text, r'data "aws_iam_policy_document" "%s"' % policy_name, 700)
        in_policy = ("aws_secretsmanager_secret.%s.arn" % OWN[name]) in policy
        if name in allowed:
            if not entry:
                problems.append("%s: %s is missing from %s" % (service, name, locals_name))
            elif entry.group(1) != OWN[name]:
                problems.append("%s: %s points at aws_secretsmanager_secret.%s, not .%s" % (service, name, entry.group(1), OWN[name]))
            if not in_policy:
                problems.append("%s: the %s policy does not list the %s secret it injects" % (service, policy_name, OWN[name]))
        else:
            if entry:
                problems.append("%s: %s must not be injected into the %s task" % (service, name, service))
            if in_policy:
                problems.append("%s: the %s policy must not list the %s secret" % (service, policy_name, OWN[name]))
    for p in problems:
        print("FAIL " + p)
    print("WIRING " + ("FAIL" if problems else "PASS (%d service x secret pairs checked)" % (len(SERVICES) * len(OWN))))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
