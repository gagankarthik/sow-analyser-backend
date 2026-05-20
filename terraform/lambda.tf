# ─── Shared Lambda layer ────────────────────────────────────────────────────────
# Build the layer first:  bash build.sh   (or .\build.ps1 on Windows)
# The script installs pip deps + copies shared/ into build/shared-layer/python/
# and zips the result to build/shared-layer.zip.

resource "aws_lambda_layer_version" "shared" {
  layer_name               = "${local.prefix}-shared"
  description              = "Shared Python deps: boto3, openai, pdfplumber, aws-lambda-powertools, ..."
  filename                 = var.layer_zip_path
  source_code_hash         = filebase64sha256(var.layer_zip_path)
  compatible_runtimes      = ["python3.12"]
  compatible_architectures = ["arm64"]
}


# ─── Pipeline Lambda (single function — all seven stages) ──────────────────────
# Step Functions injects _stage into the payload via States.JsonMerge so the
# same Lambda handles every stage.  Memory and timeout are sized for the most
# demanding stage (Textract async + GPT classify/embed/diff).

data "archive_file" "pipeline" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/pipeline"
  output_path = "${path.module}/../build/pipeline.zip"
}

locals {
  pipeline_env = {
    PROJECT_NAME                 = var.project_name
    STAGE                        = var.stage
    DDB_TABLE_NAME               = aws_dynamodb_table.main.name
    RAW_BUCKET                   = aws_s3_bucket.raw.bucket
    PROCESSED_BUCKET             = aws_s3_bucket.processed.bucket
    OPENAI_API_KEY               = var.openai_api_key
    OPENSEARCH_ENDPOINT          = aws_opensearch_domain.main.endpoint
    EMBEDDING_MODEL              = var.embedding_model
    CHAT_MODEL                   = var.chat_model
    LOG_LEVEL                    = "INFO"
    POWERTOOLS_SERVICE_NAME      = "${local.prefix}-pipeline"
    POWERTOOLS_METRICS_NAMESPACE = local.prefix
  }
}

resource "aws_cloudwatch_log_group" "pipeline" {
  name              = "/aws/lambda/${local.prefix}-pipeline"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "pipeline" {
  function_name    = "${local.prefix}-pipeline"
  description      = "Blue-IQ document ingestion pipeline — all seven stages"
  role             = aws_iam_role.pipeline_base.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.pipeline.output_path
  source_code_hash = data.archive_file.pipeline.output_base64sha256
  memory_size      = 1024
  timeout          = 600
  layers           = [aws_lambda_layer_version.shared.arn]

  environment {
    variables = local.pipeline_env
  }

  dead_letter_config {
    target_arn = aws_sqs_queue.pipeline_dlq.arn
  }

  tracing_config { mode = "Active" }

  depends_on = [aws_cloudwatch_log_group.pipeline]
}


# ─── Document API Lambda ───────────────────────────────────────────────────────

data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/api"
  output_path = "${path.module}/../build/api.zip"
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.prefix}-api"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "api" {
  function_name    = "${local.prefix}-api"
  description      = "Blue-IQ document management API (list, delete, version rollback)"
  role             = aws_iam_role.pipeline_base.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  memory_size      = 256
  timeout          = 30
  layers           = [aws_lambda_layer_version.shared.arn]

  environment {
    variables = {
      PROJECT_NAME                 = var.project_name
      STAGE                        = var.stage
      DDB_TABLE_NAME               = aws_dynamodb_table.main.name
      RAW_BUCKET                   = aws_s3_bucket.raw.bucket
      PROCESSED_BUCKET             = aws_s3_bucket.processed.bucket
      LOG_LEVEL                    = "INFO"
      POWERTOOLS_SERVICE_NAME      = "${local.prefix}-api"
      POWERTOOLS_METRICS_NAMESPACE = local.prefix
    }
  }

  tracing_config { mode = "Active" }

  depends_on = [aws_cloudwatch_log_group.api]
}

resource "aws_iam_role_policy" "api" {
  name = "api"
  role = aws_iam_role.pipeline_base.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DDBDocumentOps"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem",
          "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:BatchWriteItem",
        ]
        Resource = [aws_dynamodb_table.main.arn, "${aws_dynamodb_table.main.arn}/index/*"]
      },
    ]
  })
}

# HTTP API Gateway (v2) — lightweight, no usage plans needed for v1.
resource "aws_apigatewayv2_api" "documents" {
  name          = "${local.prefix}-docs-api"
  protocol_type = "HTTP"
  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "DELETE", "OPTIONS"]
    allow_headers = ["Content-Type", "Authorization", "x-tenant-id"]
    max_age       = 300
  }
}

resource "aws_apigatewayv2_integration" "api_lambda" {
  api_id                 = aws_apigatewayv2_api.documents.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "get_documents" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "GET /documents"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"
}

resource "aws_apigatewayv2_route" "get_document" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "GET /documents/{docId}"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"
}

resource "aws_apigatewayv2_route" "delete_document" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "DELETE /documents/{docId}"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"
}

resource "aws_apigatewayv2_route" "delete_version" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "DELETE /documents/{docId}/versions/{version}"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.documents.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api.arn
    format          = "$context.requestId $context.status $context.routeKey $context.integrationErrorMessage"
  }
}

resource "aws_lambda_permission" "apigw_api" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.documents.execution_arn}/*/*"
}


# ─── RAG Lambda (AppSync-triggered, separate package) ─────────────────────────

data "archive_file" "rag" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/rag"
  output_path = "${path.module}/../build/rag.zip"
}

resource "aws_cloudwatch_log_group" "rag" {
  name              = "/aws/lambda/${local.prefix}-rag"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "rag" {
  function_name    = "${local.prefix}-rag"
  description      = "Blue-IQ RAG resolver — backs AppSync askBluely mutation"
  role             = aws_iam_role.pipeline_base.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.rag.output_path
  source_code_hash = data.archive_file.rag.output_base64sha256
  memory_size      = 512
  timeout          = 300
  layers           = [aws_lambda_layer_version.shared.arn]

  environment {
    variables = merge(local.pipeline_env, {
      PIPELINE_STAGE           = "08_rag"
      RAG_MAX_CONTEXT_CLAUSES  = "8"
      RAG_MAX_CLAUSE_CHARS     = "1200"
      APPSYNC_GRAPHQL_ENDPOINT = ""  # wire in after AppSync API is created
    })
  }

  tracing_config { mode = "Active" }

  depends_on = [aws_cloudwatch_log_group.rag]
}
