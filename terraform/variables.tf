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
  description = "DEPRECATED (kept for backward compatibility): a key here lands in Terraform state and the Lambda environment. Leave it empty and put the key in the Secrets Manager secret (output openai_secret_arn); the Lambdas read it from there via OPENAI_SECRET_ARN."
  type        = string
  sensitive   = true
  default     = ""
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

# ─── Authentication (Cognito JWT authorizer on the documents API) ──────────────
# These IDs are NOT secrets — they are public client config. They reference the
# existing Cognito User Pool created outside Terraform. The frontend signs in
# against the same pool and sends the Cognito ID token as `Authorization:
# Bearer <token>`; the authorizer validates the signature, issuer, and audience.

variable "cognito_user_pool_id" {
  description = "Existing Cognito User Pool ID used to authorize the documents API."
  type        = string
  default     = "us-east-2_97cPE7VKm"
}

variable "cognito_client_id" {
  description = "Cognito App Client ID — the JWT audience for ID-token validation."
  type        = string
  default     = "5c3l92c8v1d3ucfn0bm37gsquq"
}

variable "allowed_origins" {
  description = "Browser origins permitted by CORS. Add each deployed frontend origin."
  type        = list(string)
  default     = ["http://localhost:3000", "https://govern.blue-iq.ai"]
}

# ─── Tenant isolation ──────────────────────────────────────────────────────────
# There is deliberately NO variable here. The tenant is the verified
# `custom:tenantId` claim, else the caller's private `u-<sub>` workspace; a
# malformed claim is a 403. No setting can put users in a shared tenant.

# ─── Model selection per task ─────────────────────────────────────────────────
# `chat_model` is the default for every text task. Each task below can be pointed
# at a different model; leave it "" to inherit `chat_model`. See README
# ("Models and accuracy settings") for what to raise for higher accuracy.

variable "extraction_model" {
  description = "Model for document-level extraction (parties, dates, money, scope). \"\" = chat_model."
  type        = string
  default     = ""
}

variable "clause_model" {
  description = "Model for per-clause labelling (type, risk, summary). \"\" = extraction_model/chat_model."
  type        = string
  default     = ""
}

variable "validation_model" {
  description = "Model for the money re-check pass. \"\" = extraction_model/chat_model."
  type        = string
  default     = ""
}

variable "rag_model" {
  description = "Model that answers Sonar chat questions. \"\" = chat_model."
  type        = string
  default     = ""
}

variable "embedding_dimensions" {
  description = "Vector size of the clause index. MUST equal what embedding_model returns; changing it needs a new index + full reprocess."
  type        = number
  default     = 1536
}

variable "embedding_send_dimensions" {
  description = "Ask the embedding model for exactly embedding_dimensions (lets a larger model fill a 1536-wide index). Only for models that accept a `dimensions` parameter."
  type        = bool
  default     = false
}

variable "llm_max_concurrency" {
  description = "Max simultaneous OpenAI requests per pipeline invocation. Lower it if the account hits rate limits."
  type        = number
  default     = 4
}

variable "classify_max_input_tokens" {
  description = "Largest document slice sent in one extraction request. Longer documents are split into overlapping windows and merged — never truncated."
  type        = number
  default     = 60000
}

variable "openai_timeout_seconds" {
  description = "Per-request timeout for chat/extraction calls."
  type        = number
  default     = 180
}

# ─── AI provider ──────────────────────────────────────────────────────────────

variable "ai_provider" {
  description = "AI provider the guardrails allow (shared/guardrails.py): \"openai\", or \"openai-zdr\" once Zero Data Retention is approved for the account."
  type        = string
  default     = "openai"
  validation {
    condition     = contains(["openai", "openai-zdr"], var.ai_provider)
    error_message = "The ai_provider must be openai or openai-zdr."
  }
}

# ─── Govern ────────────────────────────────────────────────────────────────────

variable "govern_open_admin" {
  description = "Sandbox only: treat signed-in users in no Govern Cognito group as Govern admins. Honoured ONLY when stage = dev; staging / prod always require the govern-admin / govern-reviewer / govern-leader groups."
  type        = bool
  default     = false
}

variable "govern_features" {
  description = "Govern \"Later\" features switched on for this deployment, as a comma list: routing_rules, docusign, notifications, obligations, exports, integrations (e.g. \"exports,docusign\"). Empty = all of them off. Reported to the web app by GET /govern/me."
  type        = string
  default     = "obligations"
  validation {
    condition = alltrue([
      for f in compact([for s in split(",", var.govern_features) : trimspace(s)]) :
      contains(["routing_rules", "docusign", "notifications", "obligations", "exports", "integrations"], f)
    ])
    error_message = "The govern_features list may only name routing_rules, docusign, notifications, obligations, exports and integrations."
  }
}

variable "notify_from_email" {
  description = "Verified SES sender for Govern alert email (an SES identity is created when set). Empty = email alerts off."
  type        = string
  default     = ""
}

variable "app_base_url" {
  description = "Web app base URL used in alert links."
  type        = string
  default     = "https://govern.blue-iq.ai"
}

variable "govern_log_retention_days" {
  description = "CloudWatch retention for Govern Lambda and API access logs (audit evidence; at least 365)."
  type        = number
  default     = 365
  validation {
    condition     = var.govern_log_retention_days >= 365
    error_message = "Govern logs are kept for at least 365 days."
  }
}

variable "alarm_sns_topic_arn" {
  description = "Optional SNS topic for Govern DLQ / error alarms. Empty = alarms without notifications."
  type        = string
  default     = ""
}
