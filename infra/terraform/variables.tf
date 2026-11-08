variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "priorauth-gateway"
}

variable "environment" {
  type    = string
  default = "demo"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "image_tag" {
  description = "Tag of the image in the ECR repository this stack creates (CI would push the git SHA)."
  type        = string
  default     = "latest"
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "worker_desired_count" {
  type    = number
  default = 1
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "mq_instance_type" {
  type    = string
  default = "mq.t3.micro"
}

variable "deploy_mock_payer" {
  description = "Run the mock health plan in the cluster. Set false and set payer_base_url to use a real one."
  type        = bool
  default     = true
}

variable "payer_base_url" {
  description = "Health plan endpoint when deploy_mock_payer is false."
  type        = string
  default     = ""
}

variable "fhir_base_url" {
  type    = string
  default = "https://hapi.fhir.org/baseR4"
}

variable "demo_users" {
  description = "DEMO_USERS value (name:sha256(password):roles). Empty disables the token endpoint's users."
  type        = string
  default     = ""
  sensitive   = true
}

variable "api_keys" {
  description = "API_KEYS value (name:sha256(key):roles) for partner systems."
  type        = string
  default     = ""
  sensitive   = true
}

variable "deletion_protection" {
  description = "Protect the database from terraform destroy. Off for a throwaway demo."
  type        = bool
  default     = false
}
