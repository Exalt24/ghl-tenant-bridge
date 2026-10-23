# One image, two services. The web service sits behind the load balancer. The worker is the same image started with
# `python -m relay.worker`: the relay supports a separate worker only on Postgres (config.py refuses it otherwise), which is why the database exists.

resource "aws_ecr_repository" "relay" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.ecr_force_delete

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Tags are immutable and each deploy brings a new one, so without this the repository grows for ever.
resource "aws_ecr_lifecycle_policy" "relay" {
  repository = aws_ecr_repository.relay.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the newest ${var.image_retention_count} images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = var.image_retention_count
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "relay" {
  name              = "/ecs/${var.name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# One execution role per service, so the worker never holds the ingest or admin token it does not use (config.py asks for those two only in the web role).
resource "aws_iam_role" "execution_web" {
  name_prefix        = "${var.name}-web-exec-"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role" "execution_worker" {
  name_prefix        = "${var.name}-worker-exec-"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_web_managed" {
  role       = aws_iam_role.execution_web.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy_attachment" "execution_worker_managed" {
  role       = aws_iam_role.execution_worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets_web" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = concat([
      aws_secretsmanager_secret.database_url.arn,
      aws_secretsmanager_secret.ingest_token.arn,
      aws_secretsmanager_secret.admin_token.arn,
      aws_secretsmanager_secret.relay_config.arn,
    ], values(var.vendor_secret_arns))
  }
}

data "aws_iam_policy_document" "read_secrets_worker" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = concat([
      aws_secretsmanager_secret.database_url.arn,
      aws_secretsmanager_secret.relay_config.arn,
    ], values(var.vendor_secret_arns))
  }
}

resource "aws_iam_role_policy" "execution_web_secrets" {
  name_prefix = "${var.name}-secrets-"
  role        = aws_iam_role.execution_web.id
  policy      = data.aws_iam_policy_document.read_secrets_web.json
}

resource "aws_iam_role_policy" "execution_worker_secrets" {
  name_prefix = "${var.name}-secrets-"
  role        = aws_iam_role.execution_worker.id
  policy      = data.aws_iam_policy_document.read_secrets_worker.json
}

resource "aws_ecs_cluster" "relay" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

locals {
  # The image address comes from the repository this stack creates, so the tag can be chosen before the repository exists.
  image = "${aws_ecr_repository.relay.repository_url}:${var.image_tag}"

  # Every environment variable that RELAY_CONFIG names (a Slack URL, a Meta token, a Sheets key) comes from a secret the operator created.
  vendor_secrets = [for name, arn in var.vendor_secret_arns : { name = name, valueFrom = arn }]

  secrets_web = concat([
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
    { name = "RELAY_INGEST_TOKEN", valueFrom = aws_secretsmanager_secret.ingest_token.arn },
    { name = "RELAY_ADMIN_TOKEN", valueFrom = aws_secretsmanager_secret.admin_token.arn },
    { name = "RELAY_CONFIG", valueFrom = aws_secretsmanager_secret.relay_config.arn },
  ], local.vendor_secrets)

  secrets_worker = concat([
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
    { name = "RELAY_CONFIG", valueFrom = aws_secretsmanager_secret.relay_config.arn },
  ], local.vendor_secrets)

  # HOME=/tmp because gunicorn writes a control socket under $HOME/.gunicorn, and the root filesystem is read only.
  environment = [
    { name = "RELAY_ENV", value = "prod" },
    { name = "RELAY_LIVE", value = var.relay_live },
    { name = "HOME", value = "/tmp" },
  ]

  # The root filesystem is read only (a hardening choice), so /tmp is a writable volume: gunicorn's worker heartbeat files and Python's tempfile
  # need it, and the container exits with "No usable temporary directory" without it (reproduced against the real image on 2026-10-24).
  mount_points = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]

  log_configuration = {
    logDriver = "awslogs"
    options = {
      "awslogs-group"         = aws_cloudwatch_log_group.relay.name
      "awslogs-region"        = var.aws_region
      "awslogs-stream-prefix" = "relay"
    }
  }
}

resource "aws_ecs_task_definition" "web" {
  family                   = "${var.name}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution_web.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([
    {
      name                   = "web"
      image                  = local.image
      essential              = true
      readonlyRootFilesystem = true
      user                   = "10001"
      portMappings           = [{ containerPort = 8000, protocol = "tcp" }]
      environment            = local.environment
      secrets                = local.secrets_web
      mountPoints            = local.mount_points
      logConfiguration       = local.log_configuration
      # /livez answers without the database, so a database failover does not make every task fail its check at once and get replaced together.
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=3).status == 200 else 1)\""]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 20
      }
    }
  ])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution_worker.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([
    {
      name                   = "worker"
      image                  = local.image
      essential              = true
      readonlyRootFilesystem = true
      user                   = "10001"
      command                = ["python", "-m", "relay.worker"]
      environment            = local.environment
      secrets                = local.secrets_worker
      mountPoints            = local.mount_points
      logConfiguration       = local.log_configuration
    }
  ])
}

resource "aws_ecs_service" "web" {
  name                               = "${var.name}-web"
  cluster                            = aws_ecs_cluster.relay.id
  task_definition                    = aws_ecs_task_definition.web.arn
  desired_count                      = var.web_desired_count
  launch_type                        = "FARGATE"
  health_check_grace_period_seconds  = 60
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = "web"
    container_port   = 8000
  }

  # Both listeners: with a certificate port 80 only redirects and the forwarding listener is the HTTPS one, and the target group must be attached
  # to a listener before the service is created. The IAM policies are listed because a destroy that removes them first leaves the service stuck draining.
  depends_on = [
    aws_lb_listener.http,
    aws_lb_listener.https,
    aws_iam_role_policy.execution_web_secrets,
    aws_iam_role_policy_attachment.execution_web_managed,
    # A task reads its secrets once, when it starts, so the values and the database must exist first or the first deployment fails and rolls back.
    aws_db_instance.relay,
    aws_secretsmanager_secret_version.database_url,
    aws_secretsmanager_secret_version.ingest_token,
    aws_secretsmanager_secret_version.admin_token,
    aws_secretsmanager_secret_version.relay_config,
  ]

  lifecycle {
    ignore_changes = [desired_count] # autoscaling owns it
  }
}

# Exactly one worker: the relay claims work with one conditional statement on the database clock, so a second would be safe, but one is the tested shape.
resource "aws_ecs_service" "worker" {
  name                               = "${var.name}-worker"
  cluster                            = aws_ecs_cluster.relay.id
  task_definition                    = aws_ecs_task_definition.worker.arn
  desired_count                      = 1
  launch_type                        = "FARGATE"
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  depends_on = [
    aws_iam_role_policy.execution_worker_secrets,
    aws_iam_role_policy_attachment.execution_worker_managed,
    aws_db_instance.relay,
    aws_secretsmanager_secret_version.database_url,
    aws_secretsmanager_secret_version.relay_config,
  ]
}

resource "aws_appautoscaling_target" "web" {
  service_namespace  = "ecs"
  scalable_dimension = "ecs:service:DesiredCount"
  resource_id        = "service/${aws_ecs_cluster.relay.name}/${aws_ecs_service.web.name}"
  min_capacity       = var.web_min_count
  max_capacity       = var.web_max_count
}

resource "aws_appautoscaling_policy" "web_cpu" {
  name               = "${var.name}-web-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.web.service_namespace
  scalable_dimension = aws_appautoscaling_target.web.scalable_dimension
  resource_id        = aws_appautoscaling_target.web.resource_id

  target_tracking_scaling_policy_configuration {
    target_value       = 60
    scale_in_cooldown  = 300
    scale_out_cooldown = 60

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}
