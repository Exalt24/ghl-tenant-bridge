# LIFESPAN: permanent
"""Mutation check for the Terraform guards (the same idea as relay/tests/mutants.py).

Copies the stack to a temp folder, breaks ONE guard per mutant and runs `terraform test`. Every mutant must make exactly the run named for it fail.
A mutant that survives means the test for that guard cannot fail. The unmutated copy must pass first, so a broken harness cannot pass for a caught mutant.

    python tests/mutate.py            run every mutant (about a minute after the provider is cached)
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
STACK = os.path.dirname(HERE)

# (name, file, old text, new text, the run that must fail)
MUTANTS = [
    ("database unencrypted", "data.tf", "storage_encrypted = true", "storage_encrypted = false", "database_is_private_encrypted_and_kept"),
    ("database public", "data.tf", "publicly_accessible    = false", "publicly_accessible    = true", "database_is_private_encrypted_and_kept"),
    ("database deletable", "data.tf", "deletion_protection       = var.deletion_protection", "deletion_protection       = false", "database_is_private_encrypted_and_kept"),
    ("no final snapshot", "data.tf", "skip_final_snapshot       = false", "skip_final_snapshot       = true", "database_is_private_encrypted_and_kept"),
    ("backups dropped", "data.tf", "backup_retention_period   = 7", "backup_retention_period   = 1", "database_is_private_encrypted_and_kept"),
    ("database open to the internet", "network.tf", "  to_port                      = 5432\n  referenced_security_group_id = aws_security_group.tasks.id\n}\n", "  to_port                      = 5432\n  cidr_ipv4                    = \"0.0.0.0/0\"\n}\n", "database_accepts_only_the_task_group"),
    ("database on the wrong port", "network.tf", "  description                  = \"Postgres, from the relay tasks only\"\n  ip_protocol                  = \"tcp\"\n  from_port                    = 5432", "  description                  = \"Postgres, from the relay tasks only\"\n  ip_protocol                  = \"tcp\"\n  from_port                    = 5433", "database_accepts_only_the_task_group"),
    ("web task gets a public ip", "ecs.tf", "    assign_public_ip = false\n  }\n\n  load_balancer", "    assign_public_ip = true\n  }\n\n  load_balancer", "tasks_are_private_and_only_the_balancer_reaches_them"),
    ("tasks open to the internet", "network.tf", "  to_port                      = 8000\n  referenced_security_group_id = aws_security_group.alb.id\n}\n\n# The vendors", "  to_port                      = 8000\n  cidr_ipv4                    = \"0.0.0.0/0\"\n}\n\n# The vendors", "tasks_are_private_and_only_the_balancer_reaches_them"),
    ("a secret in the plain environment", "ecs.tf", "    { name = \"RELAY_ENV\", value = \"prod\" },", "    { name = \"RELAY_ENV\", value = \"prod\" },\n    { name = \"RELAY_ADMIN_TOKEN\", value = \"literal\" },", "no_secret_is_a_plain_environment_value"),
    ("the worker also gets the admin token", "ecs.tf", "  secrets_worker = concat([\n", "  secrets_worker = concat([\n    { name = \"RELAY_ADMIN_TOKEN\", valueFrom = aws_secretsmanager_secret.admin_token.arn },\n", "the_worker_holds_only_the_secrets_it_uses"),
    ("the worker runs under the web role", "ecs.tf", "  execution_role_arn       = aws_iam_role.execution_worker.arn", "  execution_role_arn       = aws_iam_role.execution_web.arn", "the_worker_holds_only_the_secrets_it_uses"),
    ("database password stored as a plain argument", "data.tf", "  password_wo         = ephemeral.random_password.db.result\n  password_wo_version = var.secrets_version", "  password = \"hunter2hunter2\"", "passwords_are_write_only_and_never_in_state"),
    ("a token stored as a plain secret string", "data.tf", "  secret_id                = aws_secretsmanager_secret.ingest_token.id\n  secret_string_wo         = ephemeral.random_password.ingest_token.result\n  secret_string_wo_version = var.secrets_version", "  secret_id     = aws_secretsmanager_secret.ingest_token.id\n  secret_string = \"literal-token\"", "passwords_are_write_only_and_never_in_state"),
    ("worker starts the web server", "ecs.tf", "command                = [\"python\", \"-m\", \"relay.worker\"]", "command                = [\"python\", \"-m\", \"relay.app\"]", "worker_runs_the_worker_and_the_filesystem_is_read_only"),
    ("writable root filesystem", "ecs.tf", "      name                   = \"web\"\n      image                  = local.image\n      essential              = true\n      readonlyRootFilesystem = true", "      name                   = \"web\"\n      image                  = local.image\n      essential              = true\n      readonlyRootFilesystem = false", "worker_runs_the_worker_and_the_filesystem_is_read_only"),
    ("task runs as root", "ecs.tf", "      user                   = \"10001\"\n      command", "      user                   = \"0\"\n      command", "worker_runs_the_worker_and_the_filesystem_is_read_only"),
    ("no writable tmp for the web task", "ecs.tf", "      secrets                = local.secrets_web\n      mountPoints            = local.mount_points\n", "      secrets                = local.secrets_web\n", "tmp_is_a_writable_volume_under_a_read_only_root"),
    ("HOME not pointed at the volume", "ecs.tf", "    { name = \"HOME\", value = \"/tmp\" },\n", "", "tmp_is_a_writable_volume_under_a_read_only_root"),
    ("the load balancer checks the database again", "alb.tf", "    path                = \"/livez\"", "    path                = \"/health\"", "health_checks_do_not_depend_on_the_database"),
    ("no https listener with a certificate", "alb.tf", "count = var.certificate_arn == \"\" ? 0 : 1", "count = 0", "with_a_certificate_port_80_only_redirects"),
    ("port 80 forwards even with a certificate", "alb.tf", "type             = var.certificate_arn == \"\" ? \"forward\" : \"redirect\"", "type             = \"forward\"", "with_a_certificate_port_80_only_redirects"),
    ("https listener without a certificate", "alb.tf", "count = var.certificate_arn == \"\" ? 0 : 1", "count = 1", "without_a_certificate_there_is_no_https_listener"),
    ("plain http accepted by default", "alb.tf", "condition     = var.certificate_arn != \"\" || var.allow_plain_http", "condition     = var.certificate_arn != \"\" || true", "plain_http_is_refused_unless_asked_for"),
    ("image lifecycle policy changed", "ecs.tf", "countType   = \"imageCountMoreThan\"", "countType   = \"sinceImagePushed\"", "images_expire_and_the_web_tier_scales"),
    ("autoscaling floor of one task", "variables.tf", "  description = \"Autoscaling floor for the web service.\"\n  type        = number\n  default     = 2", "  description = \"Autoscaling floor for the web service.\"\n  type        = number\n  default     = 1", "images_expire_and_the_web_tier_scales"),
    ("the worker no longer gets the vendor secrets", "ecs.tf", "    { name = \"RELAY_CONFIG\", valueFrom = aws_secretsmanager_secret.relay_config.arn },\n  ], local.vendor_secrets)\n\n  # HOME=/tmp", "    { name = \"RELAY_CONFIG\", valueFrom = aws_secretsmanager_secret.relay_config.arn },\n  ], [])\n\n  # HOME=/tmp", "vendor_secrets_reach_both_services"),
    ("a dns name without a zone is accepted", "variables.tf", "condition     = (var.domain_name == \"\") == (var.route53_zone_id == \"\")", "condition     = (var.domain_name == \"\") == (var.route53_zone_id == \"\") || true", "a_dns_name_without_a_zone_is_refused"),
    ("the web service stops waiting for the secret values", "ecs.tf", "    aws_secretsmanager_secret_version.admin_token,\n    aws_secretsmanager_secret_version.relay_config,\n  ]\n\n  lifecycle", "    aws_secretsmanager_secret_version.relay_config,\n  ]\n\n  lifecycle", "script:check_dependencies.py"),
    ("the worker stops waiting for the database", "ecs.tf", "    aws_db_instance.relay,\n    aws_secretsmanager_secret_version.database_url,\n    aws_secretsmanager_secret_version.relay_config,\n  ]\n}", "    aws_secretsmanager_secret_version.database_url,\n    aws_secretsmanager_secret_version.relay_config,\n  ]\n}", "script:check_dependencies.py"),
    ("the web service stops waiting for the https listener", "ecs.tf", "    aws_lb_listener.http,\n    aws_lb_listener.https,\n", "    aws_lb_listener.http,\n", "script:check_dependencies.py"),
    ("one subnet accepted", "variables.tf", "condition     = length(var.public_subnet_ids) >= 2", "condition     = length(var.public_subnet_ids) >= 1", "subnets_in_two_zones_are_required"),
]


def tf(args, cwd, env):
    return subprocess.run(["terraform"] + args, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)


def main():
    work = tempfile.mkdtemp(prefix="tfmut_")
    cache = os.path.join(tempfile.gettempdir(), "tf_plugin_cache")
    os.makedirs(cache, exist_ok=True)
    env = dict(os.environ, TF_PLUGIN_CACHE_DIR=cache, TF_IN_AUTOMATION="1", NO_COLOR="1")
    failures = []
    try:
        def fresh():
            # A new folder each time: on Windows a finished terraform process can hold files for a moment, so reusing one path races its own cleanup.
            dst = os.path.join(tempfile.mkdtemp(prefix="stack_", dir=work), "stack")
            shutil.copytree(STACK, dst, ignore=shutil.ignore_patterns(".terraform", "*.tfstate*"))
            return dst

        base = fresh()
        init = tf(["init", "-backend=false", "-input=false"], base, env)
        if init.returncode != 0:
            print("FAIL: init\n" + init.stdout[-600:] + init.stderr[-600:])
            return 1
        ok = tf(["test", "-no-color"], base, env)
        if ok.returncode != 0:
            print("FAIL: the unmutated stack does not pass its own tests\n" + ok.stdout[-1200:])
            return 1
        dep = subprocess.run([sys.executable, "-I", os.path.join(base, "tests", "check_dependencies.py")], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if dep.returncode != 0:
            print("FAIL: the unmutated stack does not pass check_dependencies.py\n" + dep.stdout[-600:])
            return 1
        print("baseline: pass")
        for name, fname, old, new, run in MUTANTS:
            dst = fresh()
            path = os.path.join(dst, fname)
            with open(path, encoding="utf-8", newline="") as fh:
                text = fh.read().replace("\r\n", "\n")
            if text.count(old) != 1:
                failures.append("%s: the text to break matched %d times, not 1 (the stack changed under the mutant)" % (name, text.count(old)))
                continue
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text.replace(old, new))
            if run.startswith("script:"):
                # A source-level check (ordering the mocked provider cannot see): the script itself must exit non-zero on the broken copy.
                script = run.split(":", 1)[1]
                res = subprocess.run([sys.executable, "-I", os.path.join(dst, "tests", script)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                caught = res.returncode != 0
                print("%-45s %s" % (name, "caught by " + script if caught else "SURVIVED"))
                if not caught:
                    failures.append("%s: not caught by %s" % (name, script))
                continue
            tf(["init", "-backend=false", "-input=false"], dst, env)
            res = tf(["test", "-no-color"], dst, env)
            out = res.stdout + res.stderr
            caught = res.returncode != 0 and ('run "%s"... fail' % run) in out
            print("%-45s %s" % (name, "caught by " + run if caught else "SURVIVED"))
            if not caught:
                failures.append("%s: not caught by %s" % (name, run))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("MUTANTS: %d of %d caught" % (len(MUTANTS) - len(failures), len(MUTANTS)))
    for f in failures:
        print("  " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
