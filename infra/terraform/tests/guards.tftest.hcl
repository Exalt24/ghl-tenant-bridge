# Offline guard tests: `terraform test` with a mocked AWS provider, so no account, no credentials and no network.
# Each run asserts one property a reviewer would otherwise have to find by reading the plan. tests/mutate.py breaks one guard at a time in a
# copy of the stack and requires the matching run to fail, so a test that cannot fail is caught.

mock_provider "aws" {
  # The mock fills computed strings with random text, and an IAM policy must be a JSON object, so the two policy documents get a valid stand-in.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  # Likewise the provider validates ARN-shaped fields, so each resource whose arn is passed on gets a well-formed one.
  mock_resource "aws_lb" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/mock/0123456789abcdef" }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/mock/0123456789abcdef" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:mock-AbCdEf" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:cluster/mock" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock:1" }
  }
}

# The two execution roles get different, well-formed ARNs, so a task wired to the wrong role is visible to the assertions below.
override_resource {
  target          = aws_iam_role.execution_web
  override_during = apply
  values          = { arn = "arn:aws:iam::123456789012:role/web-exec" }
}

override_resource {
  target          = aws_iam_role.execution_worker
  override_during = apply
  values          = { arn = "arn:aws:iam::123456789012:role/worker-exec" }
}

variables {
  vpc_id             = "vpc-0123456789abcdef0"
  public_subnet_ids  = ["subnet-0aaaaaaaaaaaaaaa1", "subnet-0bbbbbbbbbbbbbbb2"]
  private_subnet_ids = ["subnet-0ccccccccccccccc3", "subnet-0ddddddddddddddd4"]
  image_tag          = "example"
  certificate_arn    = "arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000"
}

run "database_is_private_encrypted_and_kept" {
  command = apply

  assert {
    condition     = aws_db_instance.relay.storage_encrypted == true
    error_message = "the database storage must be encrypted"
  }
  assert {
    condition     = aws_db_instance.relay.publicly_accessible == false
    error_message = "the database must not be publicly accessible"
  }
  assert {
    condition     = aws_db_instance.relay.deletion_protection == true && aws_db_instance.relay.skip_final_snapshot == false
    error_message = "the database must have deletion protection and a final snapshot"
  }
  assert {
    condition     = aws_db_instance.relay.backup_retention_period >= 7
    error_message = "backups must be kept at least 7 days"
  }
}

run "database_accepts_only_the_task_group" {
  command = apply

  assert {
    condition     = aws_vpc_security_group_ingress_rule.db_from_tasks.cidr_ipv4 == null && aws_vpc_security_group_ingress_rule.db_from_tasks.referenced_security_group_id != null
    error_message = "the database ingress must reference the task security group, never a CIDR"
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.db_from_tasks.from_port == 5432 && aws_vpc_security_group_ingress_rule.db_from_tasks.to_port == 5432
    error_message = "the database ingress must be Postgres only"
  }
}

run "tasks_are_private_and_only_the_balancer_reaches_them" {
  command = apply

  assert {
    condition     = aws_ecs_service.web.network_configuration[0].assign_public_ip == false && aws_ecs_service.worker.network_configuration[0].assign_public_ip == false
    error_message = "no task may get a public IP"
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.tasks_from_alb.cidr_ipv4 == null && aws_vpc_security_group_ingress_rule.tasks_from_alb.referenced_security_group_id != null
    error_message = "the tasks may only be reached from the balancer's security group"
  }
}

run "no_secret_is_a_plain_environment_value" {
  command = apply

  assert {
    condition = alltrue([
      for e in jsondecode(aws_ecs_task_definition.web.container_definitions)[0].environment :
      !contains(["DATABASE_URL", "RELAY_INGEST_TOKEN", "RELAY_ADMIN_TOKEN", "RELAY_CONFIG"], e.name)
    ])
    error_message = "a secret was put in the plain environment block instead of the secrets block"
  }
  assert {
    condition = alltrue([
      for n in ["DATABASE_URL", "RELAY_INGEST_TOKEN", "RELAY_ADMIN_TOKEN", "RELAY_CONFIG"] :
      contains([for s in jsondecode(aws_ecs_task_definition.web.container_definitions)[0].secrets : s.name], n)
    ])
    error_message = "the web task must receive all four secrets from Secrets Manager"
  }
}

run "the_worker_holds_only_the_secrets_it_uses" {
  command = apply


  assert {
    condition     = join(",", sort([for s in jsondecode(aws_ecs_task_definition.worker.container_definitions)[0].secrets : s.name])) == "DATABASE_URL,RELAY_CONFIG"
    error_message = "the worker must get the database URL and the config and nothing else (no ingest or admin token)"
  }
  assert {
    condition     = aws_ecs_task_definition.worker.execution_role_arn == "arn:aws:iam::123456789012:role/worker-exec" && aws_ecs_task_definition.web.execution_role_arn == "arn:aws:iam::123456789012:role/web-exec"
    error_message = "the worker needs its own execution role so its secret access is narrower than the web task (worker ${aws_ecs_task_definition.worker.execution_role_arn}, web ${aws_ecs_task_definition.web.execution_role_arn})"
  }
}

run "passwords_are_write_only_and_never_in_state" {
  command = apply

  assert {
    condition     = aws_db_instance.relay.password == null && aws_db_instance.relay.password_wo_version == 1
    error_message = "the database password must be passed as password_wo, not password"
  }
  assert {
    condition = alltrue([
      for s in [aws_secretsmanager_secret_version.database_url, aws_secretsmanager_secret_version.ingest_token, aws_secretsmanager_secret_version.admin_token] :
      s.secret_string == null && s.secret_string_wo_version == 1
    ])
    error_message = "secret values must be write-only (secret_string_wo), not secret_string"
  }
}

run "tmp_is_a_writable_volume_under_a_read_only_root" {
  command = apply

  assert {
    condition = alltrue([
      for td in [aws_ecs_task_definition.web, aws_ecs_task_definition.worker] :
      length(td.volume) == 1 && contains([for m in jsondecode(td.container_definitions)[0].mountPoints : m.containerPath], "/tmp") && contains([for e in jsondecode(td.container_definitions)[0].environment : "${e.name}=${e.value}"], "HOME=/tmp")
    ])
    error_message = "with a read only root filesystem, /tmp must be a mounted volume and HOME must point at it, or gunicorn cannot start"
  }
}

run "health_checks_do_not_depend_on_the_database" {
  command = apply

  assert {
    condition     = aws_lb_target_group.web.health_check[0].path == "/livez" && strcontains(jsondecode(aws_ecs_task_definition.web.container_definitions)[0].healthCheck.command[1], "/livez")
    error_message = "the load balancer and the container checks must use /livez, which does not touch the database"
  }
}

run "plain_http_is_refused_unless_asked_for" {
  command = plan

  variables {
    certificate_arn = ""
  }

  expect_failures = [aws_lb_listener.http]
}

run "plain_http_when_explicitly_allowed" {
  command = apply

  variables {
    certificate_arn  = ""
    allow_plain_http = true
  }

  assert {
    condition     = aws_lb_listener.http.default_action[0].type == "forward"
    error_message = "with allow_plain_http and no certificate port 80 forwards"
  }
}

run "images_expire_and_the_web_tier_scales" {
  command = apply

  assert {
    condition     = strcontains(aws_ecr_lifecycle_policy.relay.policy, "imageCountMoreThan")
    error_message = "the image repository needs a lifecycle policy"
  }
  assert {
    condition     = aws_appautoscaling_target.web.min_capacity <= aws_appautoscaling_target.web.max_capacity && aws_appautoscaling_target.web.min_capacity >= 2
    error_message = "the web tier needs at least two tasks and a ceiling at or above the floor"
  }
}

run "worker_runs_the_worker_and_the_filesystem_is_read_only" {
  command = apply

  assert {
    condition     = jsondecode(aws_ecs_task_definition.worker.container_definitions)[0].command == ["python", "-m", "relay.worker"]
    error_message = "the worker service must start python -m relay.worker"
  }
  assert {
    condition = alltrue([
      for td in [aws_ecs_task_definition.web, aws_ecs_task_definition.worker] :
      jsondecode(td.container_definitions)[0].readonlyRootFilesystem == true && jsondecode(td.container_definitions)[0].user == "10001"
    ])
    error_message = "tasks must run as the non-root user with a read-only root filesystem"
  }
}

run "with_a_certificate_port_80_only_redirects" {
  command = apply

  assert {
    condition     = length(aws_lb_listener.https) == 1 && aws_lb_listener.http.default_action[0].type == "redirect"
    error_message = "with a certificate there must be an HTTPS listener and port 80 must redirect"
  }
}

run "without_a_certificate_there_is_no_https_listener" {
  command = apply

  variables {
    certificate_arn  = ""
    allow_plain_http = true
  }

  assert {
    condition     = length(aws_lb_listener.https) == 0 && aws_lb_listener.http.default_action[0].type == "forward"
    error_message = "without a certificate port 80 forwards and no HTTPS listener exists"
  }
}

run "subnets_in_two_zones_are_required" {
  command = plan

  variables {
    public_subnet_ids = ["subnet-0aaaaaaaaaaaaaaa1"]
  }

  expect_failures = [var.public_subnet_ids]
}

run "vendor_secrets_reach_both_services" {
  command = apply

  variables {
    vendor_secret_arns = { SLACK_URL = "arn:aws:secretsmanager:us-east-1:123456789012:secret:slack-AbCdEf" }
  }

  assert {
    condition = alltrue([
      for td in [aws_ecs_task_definition.web, aws_ecs_task_definition.worker] :
      contains([for s in jsondecode(td.container_definitions)[0].secrets : s.name], "SLACK_URL")
    ])
    error_message = "a vendor secret named in vendor_secret_arns must be injected into the web and the worker task"
  }
}

run "a_dns_name_without_a_zone_is_refused" {
  command = plan

  variables {
    domain_name = "relay.example.com"
  }

  expect_failures = [var.route53_zone_id]
}
