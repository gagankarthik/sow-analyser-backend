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

output "openai_secret_arn" {
  description = "Secrets Manager ARN for the OpenAI API key."
  value       = aws_secretsmanager_secret.openai.arn
}

output "pipeline_dlq_url" {
  description = "SQS DLQ URL for failed pipeline executions."
  value       = aws_sqs_queue.pipeline_dlq.url
}

output "state_machine_arn" {
  description = "Step Functions Express state machine ARN."
  value       = aws_sfn_state_machine.pipeline.arn
}

output "lambda_arns" {
  description = "Map of pipeline-stage-name → Lambda ARN."
  value       = { for k, fn in aws_lambda_function.pipeline : k => fn.arn }
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
