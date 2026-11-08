# AWS version of PriorAuth Gateway. Written and validated, NOT deployed. See infra/terraform/README.md.

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # For a real deployment, keep state remote and encrypted (it holds generated passwords):
  # backend "s3" {
  #   bucket         = "my-tf-state"
  #   key            = "priorauth-gateway/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "terraform-locks"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      DataClass   = "synthetic-only"
    }
  }
}
