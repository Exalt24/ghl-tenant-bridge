terraform {
  # 1.11 for write-only arguments (password_wo, secret_string_wo) and ephemeral resources, which keep the generated passwords and tokens out of state.
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # State is local until you copy backend.tf.example to backend.tf and fill it in (see ../README.md).
}

# offline_plan only exists so `terraform plan` can run with no AWS account (see ../README.md). It skips the credential and account
# lookups the provider otherwise makes at startup. It is false by default, so a real apply validates credentials as usual.
provider "aws" {
  region                      = var.aws_region
  skip_credentials_validation = var.offline_plan
  skip_requesting_account_id  = var.offline_plan
  skip_metadata_api_check     = var.offline_plan
  skip_region_validation      = var.offline_plan

  default_tags {
    tags = {
      Project     = var.name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
