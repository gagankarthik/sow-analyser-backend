# ─── Blue-IQ Govern ────────────────────────────────────────────────────────────
# The contract-workflow bounded context (docs/GOVERN_ARCHITECTURE.md):
#
#   five DynamoDB tables (one per access pattern) · a platform EventBridge bus
#   · SQS queues with DLQs · seven Lambdas · EventBridge Scheduler · Secrets
#   Manager secrets (values set out-of-band) · a customer-managed KMS key ·
#   API Gateway routes on the existing HTTP API · DLQ alarms.
#
# Every Lambda has its own role scoped to the tables / actions it uses; the
# activity log is PutItem-only for every writer.

locals {
  govern_prefix = "${local.prefix}-govern"

  # Sandbox convenience only: users in no Govern group are admins. Never on
  # outside the dev stage, whatever the variable says.
  govern_open_admin = var.govern_open_admin && var.stage == "dev"

  govern_table_names = {
    contracts = "${local.govern_prefix}-contracts"
    activity  = "${local.govern_prefix}-activity"
    config    = "${local.govern_prefix}-config"
    sync      = "${local.govern_prefix}-sync"
    metrics   = "${local.govern_prefix}-metrics"
  }

  govern_secret_names = ["teams", "docusign", "huron", "workday", "m365"]

  govern_env = {
    PROJECT_NAME                 = var.project_name
    STAGE                        = var.stage
    LOG_LEVEL                    = "INFO"
    POWERTOOLS_METRICS_NAMESPACE = local.prefix
    DDB_TABLE_NAME               = aws_dynamodb_table.main.name
    RAW_BUCKET                   = aws_s3_bucket.raw.bucket
    PROCESSED_BUCKET             = aws_s3_bucket.processed.bucket
    CONTRACTS_TABLE              = aws_dynamodb_table.govern_contracts.name
    ACTIVITY_TABLE               = aws_dynamodb_table.govern_activity.name
    CONFIG_TABLE                 = aws_dynamodb_table.govern_config.name
    SYNC_TABLE                   = aws_dynamodb_table.govern_sync.name
    METRICS_TABLE                = aws_dynamodb_table.govern_metrics.name
    EVENT_BUS_NAME               = aws_cloudwatch_event_bus.platform.name
    INTAKE_QUEUE_URL             = aws_sqs_queue.govern["intake"].url
    GOVERN_OPEN_ADMIN            = tostring(local.govern_open_admin)
    GOVERN_FEATURES              = var.govern_features
    NOTIFY_FROM_EMAIL            = var.notify_from_email
    SES_REGION                   = local.region
    APP_BASE_URL                 = var.app_base_url
    TEAMS_SECRET_ARN             = aws_secretsmanager_secret.govern["teams"].arn
    DOCUSIGN_SECRET_ARN          = aws_secretsmanager_secret.govern["docusign"].arn
    HURON_SECRET_ARN             = aws_secretsmanager_secret.govern["huron"].arn
    WORKDAY_SECRET_ARN           = aws_secretsmanager_secret.govern["workday"].arn
    M365_SECRET_ARN              = aws_secretsmanager_secret.govern["m365"].arn
  }

  # name → package directory, sizing, description
  govern_functions = {
    api        = { dir = "govern_api", memory = 512, timeout = 30, description = "Govern REST API (contracts, matrix, workflow, integrations, reports)" }
    intake     = { dir = "govern_intake", memory = 512, timeout = 120, description = "Document Analysed → contract, matrix review, revisions" }
    stream     = { dir = "govern_stream", memory = 256, timeout = 60, description = "Activity stream → EventBridge Govern.* + trend aggregates" }
    notifier   = { dir = "govern_notifier", memory = 256, timeout = 60, description = "Email (SES) and Teams alerts for Govern events" }
    sweeper    = { dir = "govern_sweeper", memory = 512, timeout = 300, description = "Hourly SLA, obligations and capture reconciliation" }
    connectors = { dir = "govern_connectors", memory = 512, timeout = 300, description = "Huron / Workday / M365 / DocuSign connector runs" }
    webhooks   = { dir = "govern_webhooks", memory = 256, timeout = 30, description = "Signed webhooks (DocuSign Connect)" }
  }

  # SQS-fed functions: queue name → consuming function, batch size
  govern_queues = {
    intake     = { function = "intake", batch = 5 }
    notify     = { function = "notifier", batch = 10 }
    connectors = { function = "connectors", batch = 5 }
  }

  govern_kms_actions = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
}


# ─── KMS (customer-managed) ───────────────────────────────────────────────────

resource "aws_kms_key" "govern" {
  description             = "${local.govern_prefix}: Govern tables and secrets"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = { Name = "${local.govern_prefix}-cmk" }
}

resource "aws_kms_alias" "govern" {
  name          = "alias/${local.govern_prefix}"
  target_key_id = aws_kms_key.govern.key_id
}


# ─── DynamoDB: one table per access pattern ───────────────────────────────────

# Contract aggregate: META + BLK# / OBL# / INC# / REVIEW#.
#   GSI1 board (T#<tenant> / <stage>#<stageEnteredAt>) · GSI2 owner queue
#   (OWN#<email> / <stageEnteredAt>) · GSI3 obligations due (T#<tenant>#OBL / <date>)
resource "aws_dynamodb_table" "govern_contracts" {
  name                        = local.govern_table_names.contracts
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "PK"
  range_key                   = "SK"
  deletion_protection_enabled = var.stage == "prod"

  dynamic "attribute" {
    for_each = ["PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "GSI3PK", "GSI3SK"]
    content {
      name = attribute.value
      type = "S"
    }
  }

  dynamic "global_secondary_index" {
    for_each = ["GSI1", "GSI2", "GSI3"]
    content {
      name            = global_secondary_index.value
      hash_key        = "${global_secondary_index.value}PK"
      range_key       = "${global_secondary_index.value}SK"
      projection_type = "ALL"
    }
  }

  point_in_time_recovery { enabled = true }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.govern.arn
  }

  tags = { Name = local.govern_table_names.contracts }
}

# Append-only audit log; its stream is the single source of Govern events.
resource "aws_dynamodb_table" "govern_activity" {
  name                        = local.govern_table_names.activity
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "PK"
  range_key                   = "SK"
  stream_enabled              = true
  stream_view_type            = "NEW_IMAGE"
  deletion_protection_enabled = var.stage == "prod"

  attribute {
    name = "PK"
    type = "S"
  }
  attribute {
    name = "SK"
    type = "S"
  }

  point_in_time_recovery { enabled = true }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.govern.arn
  }

  tags = { Name = local.govern_table_names.activity }
}

# Per-tenant configuration: matrix versions + CURRENT, SETTINGS, CONN#, TENANTS registry.
resource "aws_dynamodb_table" "govern_config" {
  name                        = local.govern_table_names.config
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "PK"
  range_key                   = "SK"
  deletion_protection_enabled = var.stage == "prod"

  attribute {
    name = "PK"
    type = "S"
  }
  attribute {
    name = "SK"
    type = "S"
  }

  point_in_time_recovery { enabled = true }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.govern.arn
  }

  tags = { Name = local.govern_table_names.config }
}

# Sync runs (TTL 400 days), capture reconciliation, external-id map (GSI1).
resource "aws_dynamodb_table" "govern_sync" {
  name         = local.govern_table_names.sync
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  dynamic "attribute" {
    for_each = ["PK", "SK", "GSI1PK", "GSI1SK"]
    content {
      name = attribute.value
      type = "S"
    }
  }

  global_secondary_index {
    name            = "GSI1"
    hash_key        = "GSI1PK"
    range_key       = "GSI1SK"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.govern.arn
  }

  tags = { Name = local.govern_table_names.sync }
}

# Daily trend counters (D#<date>, rebuildable from the activity log) and
# idempotency markers (SEEN#…, TTL).
resource "aws_dynamodb_table" "govern_metrics" {
  name         = local.govern_table_names.metrics
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }
  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.govern.arn
  }

  tags = { Name = local.govern_table_names.metrics }
}

locals {
  govern_table_arns = {
    contracts = aws_dynamodb_table.govern_contracts.arn
    activity  = aws_dynamodb_table.govern_activity.arn
    config    = aws_dynamodb_table.govern_config.arn
    sync      = aws_dynamodb_table.govern_sync.arn
    metrics   = aws_dynamodb_table.govern_metrics.arn
  }
}


# ─── Secrets Manager (created empty; values set out-of-band) ──────────────────
# Shape: {"tenants": {"<tenantId>": <value>}, "hmacKey": "..."} — see
# lambdas/shared/govern/secrets.py and docs/GOVERN_RUNBOOK.md.

resource "aws_secretsmanager_secret" "govern" {
  for_each                = toset(local.govern_secret_names)
  name                    = "${local.govern_prefix}/${each.key}"
  description             = "Govern ${each.key} credentials / keys (values set out-of-band; never in Terraform state)"
  kms_key_id              = aws_kms_key.govern.arn
  recovery_window_in_days = var.stage == "prod" ? 30 : 7
}

# The OpenAI API key, read by the pipeline and RAG Lambdas (OPENAI_SECRET_ARN).
# Its value is written by the deploy workflow / an operator, not by Terraform.
resource "aws_secretsmanager_secret" "openai" {
  name                    = "${local.prefix}/openai-api-key"
  description             = "OpenAI API key for the analysis pipeline and Sonar chat"
  kms_key_id              = aws_kms_key.govern.arn
  recovery_window_in_days = var.stage == "prod" ? 30 : 7
}


# ─── SES (optional sender identity) ───────────────────────────────────────────

resource "aws_sesv2_email_identity" "govern" {
  count          = var.notify_from_email == "" ? 0 : 1
  email_identity = var.notify_from_email
}


# ─── EventBridge platform bus ─────────────────────────────────────────────────

resource "aws_cloudwatch_event_bus" "platform" {
  name = "${local.prefix}-platform"
}

resource "aws_cloudwatch_event_rule" "document_analysed" {
  name           = "${local.govern_prefix}-document-analysed"
  description    = "Pipeline finished a document → govern-intake"
  event_bus_name = aws_cloudwatch_event_bus.platform.name
  event_pattern = jsonencode({
    source        = ["blue-iq.pipeline"]
    "detail-type" = ["Document Analysed"]
  })
}

resource "aws_cloudwatch_event_rule" "govern_notify" {
  name           = "${local.govern_prefix}-notify"
  description    = "Govern events that alert a person → notifier (notification_sent is deliberately absent)"
  event_bus_name = aws_cloudwatch_event_bus.platform.name
  event_pattern = jsonencode({
    source = ["blue-iq.govern"]
    "detail-type" = ["Govern.assigned", "Govern.reassigned", "Govern.sent_back", "Govern.approved",
    "Govern.office_approved", "Govern.escalated", "Govern.overdue"]
  })
}

resource "aws_cloudwatch_event_rule" "govern_connectors" {
  name           = "${local.govern_prefix}-connectors"
  description    = "Govern events → connectors (Huron push-back); connector / notifier entries excluded to avoid loops"
  event_bus_name = aws_cloudwatch_event_bus.platform.name
  event_pattern = jsonencode({
    source        = ["blue-iq.govern"]
    "detail-type" = [{ "anything-but" = ["Govern.sync", "Govern.conflict", "Govern.notification_sent", "Govern.comment"] }]
  })
}

locals {
  govern_rule_for_queue = {
    intake     = aws_cloudwatch_event_rule.document_analysed
    notify     = aws_cloudwatch_event_rule.govern_notify
    connectors = aws_cloudwatch_event_rule.govern_connectors
  }
}


# ─── SQS: one queue + DLQ per consumer ────────────────────────────────────────

resource "aws_sqs_queue" "govern_dlq" {
  for_each                  = merge(local.govern_queues, { stream = { function = "stream", batch = 0 } })
  name                      = "${local.govern_prefix}-${each.key}-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "govern" {
  for_each = local.govern_queues
  name     = "${local.govern_prefix}-${each.key}"
  # ≥ 6 × the consumer's timeout, so a message is not redelivered mid-run.
  visibility_timeout_seconds = 6 * local.govern_functions[each.value.function].timeout
  message_retention_seconds  = 345600 # 4 days
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.govern_dlq[each.key].arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue_policy" "govern" {
  for_each  = local.govern_queues
  queue_url = aws_sqs_queue.govern[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EventBridgeSend"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.govern[each.key].arn
        Condition = { ArnEquals = { "aws:SourceArn" = local.govern_rule_for_queue[each.key].arn } }
      },
      {
        Sid       = "DenyNonSSL"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:*"
        Resource  = aws_sqs_queue.govern[each.key].arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
}

resource "aws_cloudwatch_event_target" "govern" {
  for_each       = local.govern_queues
  rule           = local.govern_rule_for_queue[each.key].name
  event_bus_name = aws_cloudwatch_event_bus.platform.name
  arn            = aws_sqs_queue.govern[each.key].arn
  retry_policy {
    maximum_event_age_in_seconds = 86400
    maximum_retry_attempts       = 185
  }
}


# ─── Lambda functions ─────────────────────────────────────────────────────────

data "archive_file" "govern" {
  for_each    = local.govern_functions
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/${each.value.dir}"
  output_path = "${path.module}/../build/${each.value.dir}.zip"
  excludes    = ["__pycache__"]
}

resource "aws_cloudwatch_log_group" "govern" {
  for_each          = local.govern_functions
  name              = "/aws/lambda/${local.govern_prefix}-${each.key}"
  retention_in_days = var.govern_log_retention_days
}

resource "aws_iam_role" "govern" {
  for_each           = local.govern_functions
  name               = "${local.govern_prefix}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy_attachment" "govern_logs" {
  for_each   = local.govern_functions
  role       = aws_iam_role.govern[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "govern_xray" {
  for_each   = local.govern_functions
  role       = aws_iam_role.govern[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_lambda_function" "govern" {
  for_each         = local.govern_functions
  function_name    = "${local.govern_prefix}-${each.key}"
  description      = each.value.description
  role             = aws_iam_role.govern[each.key].arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.govern[each.key].output_path
  source_code_hash = data.archive_file.govern[each.key].output_base64sha256
  memory_size      = each.value.memory
  timeout          = each.value.timeout
  layers           = [aws_lambda_layer_version.shared.arn]

  environment {
    variables = merge(local.govern_env, { POWERTOOLS_SERVICE_NAME = "${local.govern_prefix}-${each.key}" })
  }

  tracing_config { mode = "Active" }

  depends_on = [aws_cloudwatch_log_group.govern]
}


# ─── Least-privilege policies per function ────────────────────────────────────

locals {
  docs_table_arns     = [aws_dynamodb_table.main.arn, "${aws_dynamodb_table.main.arn}/index/*"]
  govern_secret_arns  = { for k, s in aws_secretsmanager_secret.govern : k => s.arn }
  ddb_contracts_write = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:BatchGetItem"]

  govern_statements = {
    api = [
      { Sid = "DocumentsReadAndLifecycle", Effect = "Allow", Resource = local.docs_table_arns,
      Action = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:BatchGetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"] },
      { Sid = "Contracts", Effect = "Allow", Action = local.ddb_contracts_write,
      Resource = [local.govern_table_arns.contracts, "${local.govern_table_arns.contracts}/index/*"] },
      { Sid = "ActivityAppendAndRead", Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:Query"], Resource = local.govern_table_arns.activity },
      { Sid = "Config", Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"], Resource = local.govern_table_arns.config },
      { Sid = "Sync", Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Query"],
      Resource = [local.govern_table_arns.sync, "${local.govern_table_arns.sync}/index/*"] },
      { Sid = "MetricsRead", Effect = "Allow", Action = ["dynamodb:BatchGetItem", "dynamodb:GetItem"], Resource = local.govern_table_arns.metrics },
      { Sid = "ProcessedArtefacts", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${aws_s3_bucket.processed.arn}/*" },
      { Sid = "SignRevisionUploads", Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.raw.arn}/tenants/*" },
      { Sid = "IntegrationSecrets", Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"],
      Resource = values(local.govern_secret_arns) },
    ]
    intake = [
      { Sid = "Documents", Effect = "Allow", Resource = local.docs_table_arns,
      Action = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem"] },
      { Sid = "Contracts", Effect = "Allow", Action = local.ddb_contracts_write,
      Resource = [local.govern_table_arns.contracts, "${local.govern_table_arns.contracts}/index/*"] },
      { Sid = "ActivityAppendOnly", Effect = "Allow", Action = ["dynamodb:PutItem"], Resource = local.govern_table_arns.activity },
      { Sid = "Config", Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"], Resource = local.govern_table_arns.config },
      { Sid = "ExternalIds", Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:DeleteItem"], Resource = local.govern_table_arns.sync },
      { Sid = "ProcessedArtefacts", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${aws_s3_bucket.processed.arn}/*" },
      { Sid = "Queue", Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"],
      Resource = aws_sqs_queue.govern["intake"].arn },
    ]
    stream = [
      { Sid = "ActivityStream", Effect = "Allow", Action = ["dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:DescribeStream", "dynamodb:ListStreams"],
      Resource = aws_dynamodb_table.govern_activity.stream_arn },
      { Sid = "MetricsCounters", Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"], Resource = local.govern_table_arns.metrics },
      { Sid = "PublishGovernEvents", Effect = "Allow", Action = ["events:PutEvents"], Resource = aws_cloudwatch_event_bus.platform.arn },
      { Sid = "FailureDestination", Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.govern_dlq["stream"].arn },
    ]
    notifier = [
      { Sid = "ContractsRead", Effect = "Allow", Action = ["dynamodb:GetItem"], Resource = local.govern_table_arns.contracts },
      { Sid = "ConfigRead", Effect = "Allow", Action = ["dynamodb:GetItem"], Resource = local.govern_table_arns.config },
      { Sid = "ActivityAppendOnly", Effect = "Allow", Action = ["dynamodb:PutItem"], Resource = local.govern_table_arns.activity },
      { Sid = "SentMarkers", Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:DeleteItem"], Resource = local.govern_table_arns.metrics },
      { Sid = "Email", Effect = "Allow", Action = ["ses:SendEmail"], Resource = "arn:aws:ses:${local.region}:${local.account_id}:identity/*" },
      { Sid = "TeamsWebhook", Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = local.govern_secret_arns["teams"] },
      { Sid = "Queue", Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"],
      Resource = aws_sqs_queue.govern["notify"].arn },
    ]
    sweeper = [
      { Sid = "DocumentsList", Effect = "Allow", Action = ["dynamodb:Query", "dynamodb:UpdateItem"], Resource = local.docs_table_arns },
      { Sid = "Contracts", Effect = "Allow", Action = local.ddb_contracts_write,
      Resource = [local.govern_table_arns.contracts, "${local.govern_table_arns.contracts}/index/*"] },
      { Sid = "ActivityAppendOnly", Effect = "Allow", Action = ["dynamodb:PutItem"], Resource = local.govern_table_arns.activity },
      { Sid = "ConfigRead", Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:Query"], Resource = local.govern_table_arns.config },
      { Sid = "Reconcile", Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem"], Resource = local.govern_table_arns.sync },
      { Sid = "Requeue", Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.govern["intake"].arn },
    ]
    connectors = [
      { Sid = "Contracts", Effect = "Allow", Action = local.ddb_contracts_write,
      Resource = [local.govern_table_arns.contracts, "${local.govern_table_arns.contracts}/index/*"] },
      { Sid = "ActivityAppendOnly", Effect = "Allow", Action = ["dynamodb:PutItem"], Resource = local.govern_table_arns.activity },
      { Sid = "Config", Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"], Resource = local.govern_table_arns.config },
      { Sid = "Sync", Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Query"],
      Resource = [local.govern_table_arns.sync, "${local.govern_table_arns.sync}/index/*"] },
      { Sid = "ConnectorSecrets", Effect = "Allow", Action = ["secretsmanager:GetSecretValue"],
      Resource = [for k in ["huron", "workday", "m365", "docusign"] : local.govern_secret_arns[k]] },
      { Sid = "Queue", Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"],
      Resource = aws_sqs_queue.govern["connectors"].arn },
    ]
    webhooks = [
      { Sid = "Documents", Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem"], Resource = local.docs_table_arns },
      { Sid = "Contracts", Effect = "Allow", Action = local.ddb_contracts_write, Resource = local.govern_table_arns.contracts },
      { Sid = "ActivityAppendOnly", Effect = "Allow", Action = ["dynamodb:PutItem"], Resource = local.govern_table_arns.activity },
      { Sid = "ConfigRead", Effect = "Allow", Action = ["dynamodb:GetItem"], Resource = local.govern_table_arns.config },
      { Sid = "ReplayMarkers", Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:DeleteItem"], Resource = local.govern_table_arns.metrics },
      { Sid = "SignedDocument", Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.processed.arn}/*" },
      { Sid = "DocuSignKey", Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = local.govern_secret_arns["docusign"] },
    ]
  }
}

resource "aws_iam_role_policy" "govern" {
  for_each = local.govern_functions
  name     = "govern-${each.key}"
  role     = aws_iam_role.govern[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.govern_statements[each.key], [{
      Sid      = "GovernKey"
      Effect   = "Allow"
      Action   = local.govern_kms_actions
      Resource = aws_kms_key.govern.arn
    }])
  })
}


# ─── Triggers ─────────────────────────────────────────────────────────────────

resource "aws_lambda_event_source_mapping" "govern_sqs" {
  for_each                           = local.govern_queues
  event_source_arn                   = aws_sqs_queue.govern[each.key].arn
  function_name                      = aws_lambda_function.govern[each.value.function].arn
  batch_size                         = each.value.batch
  maximum_batching_window_in_seconds = 5
  function_response_types            = ["ReportBatchItemFailures"]
  scaling_config {
    maximum_concurrency = 5
  }
  depends_on = [aws_iam_role_policy.govern]
}

resource "aws_lambda_event_source_mapping" "govern_activity_stream" {
  event_source_arn               = aws_dynamodb_table.govern_activity.stream_arn
  function_name                  = aws_lambda_function.govern["stream"].arn
  starting_position              = "TRIM_HORIZON"
  batch_size                     = 50
  bisect_batch_on_function_error = true
  maximum_retry_attempts         = 10
  function_response_types        = ["ReportBatchItemFailures"]
  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.govern_dlq["stream"].arn
    }
  }
  depends_on = [aws_iam_role_policy.govern]
}

data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "govern_scheduler" {
  name               = "${local.govern_prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

resource "aws_iam_role_policy" "govern_scheduler" {
  name = "invoke"
  role = aws_iam_role.govern_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = [aws_lambda_function.govern["sweeper"].arn, aws_lambda_function.govern["connectors"].arn]
    }]
  })
}

resource "aws_scheduler_schedule" "govern_sweeper" {
  name                = "${local.govern_prefix}-sweeper-hourly"
  schedule_expression = "rate(1 hour)"
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_lambda_function.govern["sweeper"].arn
    role_arn = aws_iam_role.govern_scheduler.arn
    input    = jsonencode({ trigger = "schedule" })
    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }
  }
}

resource "aws_scheduler_schedule" "govern_connectors" {
  name                         = "${local.govern_prefix}-connectors-daily"
  schedule_expression          = "cron(0 6 * * ? *)"
  schedule_expression_timezone = "America/New_York"
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_lambda_function.govern["connectors"].arn
    role_arn = aws_iam_role.govern_scheduler.arn
    input    = jsonencode({ trigger = "schedule" })
    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }
  }
}


# ─── API Gateway routes (existing HTTP API) ───────────────────────────────────

resource "aws_apigatewayv2_integration" "govern_api" {
  api_id                 = aws_apigatewayv2_api.documents.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.govern["api"].invoke_arn
  payload_format_version = "2.0"
}

locals {
  govern_routes = toset([
    "GET /govern/me",
    "GET /contracts", "POST /contracts",
    "GET /contracts/{id}", "PATCH /contracts/{id}",
    "POST /contracts/{id}/actions", "POST /contracts/{id}/rescore",
    "POST /contracts/{id}/blockers", "PATCH /contracts/{id}/blockers/{blockerId}",
    "POST /contracts/{id}/obligations", "PATCH /contracts/{id}/obligations/{oblId}",
    "PUT /contracts/{id}/income", "GET /contracts/{id}/revision-upload-url",
    "GET /matrix", "PUT /matrix", "POST /matrix/import", "GET /matrix/versions/{n}",
    "GET /workflow/settings", "PUT /workflow/settings",
    "GET /integrations", "GET /integrations/sync-log", "GET /integrations/unmatched",
    "PUT /integrations/{id}", "POST /integrations/{id}/sync",
    "GET /reports/trends", "GET /reports/capture",
  ])
}

resource "aws_apigatewayv2_route" "govern" {
  for_each           = local.govern_routes
  api_id             = aws_apigatewayv2_api.documents.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.govern_api.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_lambda_permission" "apigw_govern_api" {
  statement_id  = "AllowAPIGatewayInvokeGovern"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.govern["api"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.documents.execution_arn}/*/*"
}

# Signed webhooks: NO JWT authorizer — the Lambda verifies the provider's HMAC.
resource "aws_apigatewayv2_integration" "govern_webhooks" {
  api_id                 = aws_apigatewayv2_api.documents.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.govern["webhooks"].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "govern_webhooks" {
  api_id             = aws_apigatewayv2_api.documents.id
  route_key          = "POST /webhooks/{provider}"
  target             = "integrations/${aws_apigatewayv2_integration.govern_webhooks.id}"
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "apigw_govern_webhooks" {
  statement_id  = "AllowAPIGatewayInvokeWebhooks"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.govern["webhooks"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.documents.execution_arn}/*/POST/webhooks/*"
}


# ─── Alarms: anything in a DLQ needs a person ─────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "govern_dlq" {
  for_each            = aws_sqs_queue.govern_dlq
  alarm_name          = "${each.value.name}-not-empty"
  alarm_description   = "Govern ${each.key}: messages failed every retry and are waiting in the DLQ (see docs/GOVERN_RUNBOOK.md)"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = each.value.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_sns_topic_arn == "" ? [] : [var.alarm_sns_topic_arn]
  ok_actions          = var.alarm_sns_topic_arn == "" ? [] : [var.alarm_sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "govern_errors" {
  for_each            = local.govern_functions
  alarm_name          = "${local.govern_prefix}-${each.key}-errors"
  alarm_description   = "Govern ${each.key} Lambda errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.govern[each.key].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_sns_topic_arn == "" ? [] : [var.alarm_sns_topic_arn]
}
