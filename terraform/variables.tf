variable "project_name" {
  description = "Project identifier used to prefix all resource names."
  type        = string
  default     = "blue-iq-sow"
}

variable "stage" {
  description = "Deployment stage (dev / staging / prod)."
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-2"
}

variable "openai_api_key" {
  description = "OpenAI API key injected into Lambda environment. Set via TF_VAR_openai_api_key or GitHub Actions secret."
  type        = string
  sensitive   = true
}

variable "opensearch_instance_type" {
  description = "OpenSearch data-node instance type."
  type        = string
  default     = "t3.small.search"
}

variable "opensearch_volume_gb" {
  description = "EBS volume size per OpenSearch node (GiB)."
  type        = number
  default     = 10
}

variable "embedding_model" {
  description = "OpenAI embedding model."
  type        = string
  default     = "text-embedding-3-small"
}

variable "chat_model" {
  description = "OpenAI chat model."
  type        = string
  default     = "gpt-4.1-mini"
}

variable "log_retention_days" {
  description = "CloudWatch log group retention in days."
  type        = number
  default     = 30
}

# Pre-built layer zip path (see build.sh).  Terraform reads this file at plan
# time; run build.sh once before `terraform apply`.
variable "layer_zip_path" {
  description = "Path to the pre-built shared-layer.zip produced by build.sh."
  type        = string
  default     = "../build/shared-layer.zip"
}
