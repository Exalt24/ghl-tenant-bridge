variable "aws_region" {
  description = "Region for every resource."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "ghl-relay"
}

variable "environment" {
  description = "Value of the Environment tag."
  type        = string
  default     = "prod"
}

variable "vpc_id" {
  description = "An existing VPC. The stack does not create networking, so it never fights the account's own."
  type        = string
}

variable "public_subnet_ids" {
  description = "Subnets for the load balancer (at least two availability zones)."
  type        = list(string)

  validation {
    condition     = length(var.public_subnet_ids) >= 2
    error_message = "An application load balancer needs subnets in at least two availability zones."
  }
}

variable "private_subnet_ids" {
  description = "Subnets for the Fargate tasks and the database (at least two availability zones, with a NAT route out for the vendor calls)."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "The database subnet group needs subnets in at least two availability zones."
  }
}

variable "image_tag" {
  description = "The tag of the relay image in the repository this stack creates, for example 2026-10-24. Build it from relay/Dockerfile and push it after the first targeted apply (see ../README.md). The repository address comes from the stack, so the tag can be chosen before the repository exists."
  type        = string

  validation {
    condition     = length(var.image_tag) > 0
    error_message = "image_tag must not be empty: the image reference would end in a colon and fail only at apply."
  }
}

variable "vendor_secret_arns" {
  description = "Secrets Manager ARNs for the environment variables that RELAY_CONFIG names (a Slack webhook URL, a Meta token, a Sheets key). Map the variable name to the secret ARN; both services receive them and may read exactly these secrets. The relay refuses to start if a variable its config names is missing."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for arn in values(var.vendor_secret_arns) : can(regex("^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+$", arn))])
    error_message = "Each vendor secret must be a plain Secrets Manager ARN with no :json-key:version suffix, because the same string is also the IAM resource and an ARN with a suffix would match nothing. The secrets must use the default AWS managed key; a customer managed key also needs kms:Decrypt on the execution roles."
  }
}

variable "certificate_arn" {
  description = "An ACM certificate for HTTPS. Required unless allow_plain_http is true, because the ingest token travels in a header."
  type        = string
  default     = ""
}

variable "allow_plain_http" {
  description = "Accept an empty certificate_arn and serve plain HTTP on port 80. Only for a throwaway test stack."
  type        = bool
  default     = false
}

variable "domain_name" {
  description = "Optional. With route53_zone_id, creates an alias record for the load balancer. The certificate must cover this name."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Optional hosted zone for domain_name. Set both or neither."
  type        = string
  default     = ""

  validation {
    condition     = (var.domain_name == "") == (var.route53_zone_id == "")
    error_message = "Set domain_name and route53_zone_id together, or neither: with only one of them no DNS record is created."
  }
}

variable "relay_live" {
  description = "RELAY_LIVE. The relay refuses a non-local vendor URL unless this is 1."
  type        = string
  default     = "0"

  validation {
    condition     = contains(["0", "1"], var.relay_live)
    error_message = "relay_live must be \"0\" or \"1\"."
  }
}

variable "web_desired_count" {
  description = "Starting number of web tasks. Autoscaling takes over between web_min_count and web_max_count."
  type        = number
  default     = 2

  validation {
    condition     = var.web_desired_count >= var.web_min_count && var.web_desired_count <= var.web_max_count
    error_message = "web_desired_count must sit between web_min_count and web_max_count."
  }
}

variable "web_min_count" {
  description = "Autoscaling floor for the web service."
  type        = number
  default     = 2

  validation {
    condition     = var.web_min_count >= 1
    error_message = "web_min_count must be at least 1: with no web task the load balancer has nothing to send a webhook to."
  }
}

variable "web_max_count" {
  description = "Autoscaling ceiling for the web service."
  type        = number
  default     = 6

  validation {
    condition     = var.web_max_count >= var.web_min_count
    error_message = "web_max_count must be at least web_min_count."
  }
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  description = "RDS storage in GB."
  type        = number
  default     = 20
}

variable "log_retention_days" {
  description = "CloudWatch retention for the container logs."
  type        = number
  default     = 30
}

variable "image_retention_count" {
  description = "How many images the ECR lifecycle policy keeps."
  type        = number
  default     = 20
}

variable "deletion_protection" {
  description = "Deletion protection on the database and the load balancer. Set false in a separate apply before a destroy."
  type        = bool
  default     = true
}

variable "final_snapshot_suffix" {
  description = "Suffix of the database's final snapshot name. Change it for each create and destroy cycle, because a snapshot name can be used once."
  type        = string
  default     = "1"
}

variable "ecr_force_delete" {
  description = "Let a destroy delete the image repository even when it holds images."
  type        = bool
  default     = false
}

variable "secrets_version" {
  description = "The passwords and tokens are write-only and generated fresh on every run, so Terraform only sends them when this number changes. Raise it to rotate all of them together."
  type        = number
  default     = 1
}

variable "offline_plan" {
  description = "Skip the provider's AWS credential and account lookups so a plan runs with no AWS account. Never set this for an apply."
  type        = bool
  default     = false
}
