output "raw_bucket_name" {
  description = "S3 bucket for raw document uploads."
  value       = aws_s3_bucket.raw.bucket
}

output "processed_bucket_name" {
  description = "S3 bucket for processed pipeline artefacts."
  value       = aws_s3_bucket.processed.bucket
}

output "dynamodb_table_name" {
  description = "DynamoDB single-table name."
  value       = aws_dynamodb_table.main.name
}

output "opensearch_endpoint" {
  description = "OpenSearch HTTPS endpoint (no protocol prefix)."
  value       = aws_opensearch_domain.main.endpoint
}

output "pipeline_dlq_url" {
  description = "SQS DLQ URL for failed pipeline executions."
  value       = aws_sqs_queue.pipeline_dlq.url
}

output "state_machine_arn" {
  description = "Step Functions Express state machine ARN."
  value       = aws_sfn_state_machine.pipeline.arn
}

output "pipeline_lambda_arn" {
  description = "Single pipeline Lambda ARN (handles all seven stages)."
  value       = aws_lambda_function.pipeline.arn
}

output "rag_lambda_arn" {
  description = "RAG Lambda ARN (wire into AppSync as a direct resolver)."
  value       = aws_lambda_function.rag.arn
}

output "shared_layer_arn" {
  description = "Shared Python Lambda layer ARN."
  value       = aws_lambda_layer_version.shared.arn
}

output "documents_api_url" {
  description = "HTTP API Gateway base URL for the document management API."
  value       = aws_apigatewayv2_api.documents.api_endpoint
}

output "api_lambda_arn" {
  description = "Document API Lambda ARN."
  value       = aws_lambda_function.api.arn
}

# ─── Govern ────────────────────────────────────────────────────────────────────

output "govern_table_names" {
  description = "The five Govern DynamoDB tables."
  value = {
    contracts = aws_dynamodb_table.govern_contracts.name
    activity  = aws_dynamodb_table.govern_activity.name
    config    = aws_dynamodb_table.govern_config.name
    sync      = aws_dynamodb_table.govern_sync.name
    metrics   = aws_dynamodb_table.govern_metrics.name
  }
}

output "platform_event_bus_name" {
  description = "EventBridge bus carrying Document Analysed and Govern.* events."
  value       = aws_cloudwatch_event_bus.platform.name
}

output "govern_queue_urls" {
  description = "Govern SQS queues (intake, notify, connectors)."
  value       = { for k, q in aws_sqs_queue.govern : k => q.url }
}

output "govern_dlq_urls" {
  description = "Govern dead-letter queues (alarmed)."
  value       = { for k, q in aws_sqs_queue.govern_dlq : k => q.url }
}

output "govern_secret_arns" {
  description = "Secrets to fill out-of-band (see docs/GOVERN_RUNBOOK.md)."
  value       = { for k, s in aws_secretsmanager_secret.govern : k => s.arn }
}

output "openai_secret_arn" {
  description = "Secrets Manager secret holding the OpenAI API key."
  value       = aws_secretsmanager_secret.openai.arn
}

output "govern_kms_key_arn" {
  description = "Customer-managed key for the Govern tables and secrets."
  value       = aws_kms_key.govern.arn
}

output "docusign_webhook_url" {
  description = "URL to configure in DocuSign Connect."
  value       = "${aws_apigatewayv2_api.documents.api_endpoint}/webhooks/docusign"
}
