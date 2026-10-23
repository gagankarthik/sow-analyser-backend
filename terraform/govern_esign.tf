# ─────────────────────────────────────────────────────────────────────────────
# Govern e-signature (docs/GOVERN_ESIGN.md)
#
#   KMS signing key (RSA 3072, SIGN_VERIFY) · seal queue + DLQ → govern-seal
#   public routes /sign/{token}… → govern-signing (no JWT: the token is the credential)
#   CORS on the processed bucket for "sign a different PDF" uploads (esign/uploads/…)
#
# The two functions themselves (govern-signing, govern-seal), their roles and
# their least-privilege statements live with the other Govern functions in
# govern.tf (local.govern_functions / local.govern_statements).
# ─────────────────────────────────────────────────────────────────────────────


# ─── The signing key: the private key never leaves KMS ───────────────────────

resource "aws_kms_key" "govern_signing" {
  description              = "${local.govern_prefix}: seals documents signed with Govern e-signature"
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "RSA_3072"
  deletion_window_in_days  = 30
  tags                     = { Name = "${local.govern_prefix}-signing" }
}

resource "aws_kms_alias" "govern_signing" {
  name          = "alias/${local.govern_prefix}-signing"
  target_key_id = aws_kms_key.govern_signing.key_id
}


# ─── Seal queue: completed envelopes waiting to be sealed ────────────────────

resource "aws_sqs_queue" "govern_seal_dlq" {
  name                      = "${local.govern_prefix}-seal-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "govern_seal" {
  name = "${local.govern_prefix}-seal"
  # ≥ 6 × govern-seal's timeout, so a message is not redelivered mid-run.
  visibility_timeout_seconds = 6 * local.govern_functions["seal"].timeout
  message_retention_seconds  = 345600 # 4 days
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.govern_seal_dlq.arn
    maxReceiveCount     = 3 # esign.MAX_SEAL_ATTEMPTS: the third failure marks the envelope "failed"
  })
}

resource "aws_sqs_queue_policy" "govern_seal" {
  queue_url = aws_sqs_queue.govern_seal.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyNonSSL"
      Effect    = "Deny"
      Principal = "*"
      Action    = "sqs:*"
      Resource  = aws_sqs_queue.govern_seal.arn
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_lambda_event_source_mapping" "govern_seal" {
  event_source_arn        = aws_sqs_queue.govern_seal.arn
  function_name           = aws_lambda_function.govern["seal"].arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]
  scaling_config {
    maximum_concurrency = 5
  }
  depends_on = [aws_iam_role_policy.govern]
}

resource "aws_cloudwatch_metric_alarm" "govern_seal_dlq" {
  alarm_name          = "${local.govern_prefix}-seal-dlq-not-empty"
  alarm_description   = "A signed envelope could not be sealed after three tries (docs/GOVERN_RUNBOOK.md)."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.govern_seal_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}


# ─── Public signer routes: NO JWT authorizer ─────────────────────────────────
# The token in the path is the credential (hashed and looked up by key in
# govern-signing). Throttled per route in the stage (lambda.tf); each link also
# caps its own opens. Access logs record $context.routeKey, never the token.

resource "aws_apigatewayv2_integration" "govern_signing" {
  api_id                 = aws_apigatewayv2_api.documents.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.govern["signing"].invoke_arn
  payload_format_version = "2.0"
}

locals {
  govern_signing_routes = toset([
    "GET /sign/{token}", "POST /sign/{token}/complete", "POST /sign/{token}/decline", "GET /sign/{token}/download",
  ])
}

resource "aws_apigatewayv2_route" "govern_signing" {
  for_each           = local.govern_signing_routes
  api_id             = aws_apigatewayv2_api.documents.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.govern_signing.id}"
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "apigw_govern_signing" {
  statement_id  = "AllowAPIGatewayInvokeSigning"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.govern["signing"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.documents.execution_arn}/*/*/sign/*"
}


# ─── Uploads of a different PDF to sign go straight from the browser ─────────

resource "aws_s3_bucket_cors_configuration" "processed" {
  bucket = aws_s3_bucket.processed.id
  cors_rule {
    allowed_origins = var.allowed_origins
    allowed_methods = ["PUT", "GET", "HEAD"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}


output "govern_signing_key_arn" {
  description = "KMS key that seals Govern e-signature documents (public key → certificate; see docs/GOVERN_ESIGN.md §4)."
  value       = aws_kms_key.govern_signing.arn
}

output "govern_seal_dlq_url" {
  description = "Dead-letter queue for envelopes that could not be sealed."
  value       = aws_sqs_queue.govern_seal_dlq.url
}
