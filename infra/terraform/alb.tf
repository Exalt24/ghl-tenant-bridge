resource "aws_lb" "relay" {
  name_prefix                = "relay-"
  load_balancer_type         = "application"
  internal                   = false
  subnets                    = var.public_subnet_ids
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
  enable_deletion_protection = var.deletion_protection
}

# /livez, not /health: the load balancer's check also decides whether ECS replaces a task, and /health fails whenever the database does.
resource "aws_lb_target_group" "web" {
  name_prefix = "relay-"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  deregistration_delay = 30

  health_check {
    path                = "/livez"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  lifecycle {
    create_before_destroy = true
  }
}

# With a certificate port 80 only redirects and the relay is reached over HTTPS. Without one, port 80 forwards, and only when allow_plain_http says so,
# because the ingest token is a bearer header and would cross the internet in clear text.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.relay.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = var.certificate_arn == "" ? "forward" : "redirect"
    target_group_arn = var.certificate_arn == "" ? aws_lb_target_group.web.arn : null

    dynamic "redirect" {
      for_each = var.certificate_arn == "" ? [] : [1]
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }

  lifecycle {
    precondition {
      condition     = var.certificate_arn != "" || var.allow_plain_http
      error_message = "Set certificate_arn, or set allow_plain_http = true for a throwaway test stack. The ingest token is sent as a bearer header."
    }
  }
}

resource "aws_lb_listener" "https" {
  count = var.certificate_arn == "" ? 0 : 1

  load_balancer_arn = aws_lb.relay.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

resource "aws_route53_record" "relay" {
  count = var.domain_name != "" && var.route53_zone_id != "" ? 1 : 0

  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_lb.relay.dns_name
    zone_id                = aws_lb.relay.zone_id
    evaluate_target_health = true
  }
}
