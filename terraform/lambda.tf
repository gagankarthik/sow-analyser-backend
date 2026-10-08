# ─── Shared Lambda layer ────────────────────────────────────────────────────────
# Build the layer first:  bash build.sh   (or .\build.ps1 on Windows)
# The script installs pip deps + copies shared/ into build/shared-layer/python/
# and zips the result to build/shared-layer.zip.

# The layer zip exceeds Lambda's 70 MB direct-upload limit (base64-inflated),
# so publish it from S3 instead of inline. The object key embeds the content
# hash so a changed zip uploads a new object and forces a new layer version.
resource "aws_s3_object" "shared_layer" {
  bucket = aws_s3_bucket.processed.id
  key    = "lambda-layers/shared-layer-${filebase64sha256(var.layer_zip_path)}.zip"
  source = var.layer_zip_path
  etag   = filemd5(var.layer_zip_path)
}

resource "aws_lambda_layer_version" "shared" {
  layer_name               = "${local.prefix}-shared"
  description              = "Shared Python deps: boto3, openai, pdfplumber, aws-lambda-powertools, ..."
  s3_bucket                = aws_s3_object.shared_layer.bucket
  s3_key                   = aws_s3_object.shared_layer.key
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
    PROJECT_NAME     = var.project_name
    STAGE            = var.stage
    DDB_TABLE_NAME   = aws_dynamodb_table.main.name
    RAW_BUCKET       = aws_s3_bucket.raw.bucket
    PROCESSED_BUCKET = aws_s3_bucket.processed.bucket
    # The key is read from Secrets Manager; OPENAI_API_KEY is only a legacy
    # override (empty unless var.openai_api_key is still set).
    OPENAI_SECRET_ARN   = aws_secretsmanager_secret.openai.arn
    OPENAI_API_KEY      = var.openai_api_key
    AI_PROVIDER         = var.ai_provider
    OPENSEARCH_ENDPOINT = aws_opensearch_domain.main.endpoint
    EMBEDDING_MODEL     = var.embedding_model
    CHAT_MODEL          = var.chat_model
    # Per-task model overrides ("" = inherit CHAT_MODEL) and the tuning knobs
    # that bound cost/latency. Defaults live in lambdas/shared/config.py.
    EXTRACTION_MODEL             = var.extraction_model
    CLAUSE_MODEL                 = var.clause_model
    VALIDATION_MODEL             = var.validation_model
    RAG_MODEL                    = var.rag_model
    EMBEDDING_DIMENSIONS         = tostring(var.embedding_dimensions)
    EMBEDDING_SEND_DIMENSIONS    = tostring(var.embedding_send_dimensions)
    LLM_MAX_CONCURRENCY          = tostring(var.llm_max_concurrency)
    CLASSIFY_MAX_INPUT_TOKENS    = tostring(var.classify_max_input_tokens)
    OPENAI_TIMEOUT_S             = tostring(var.openai_timeout_seconds)
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
  role             = aws_iam_role.api.arn
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
      PROJECT_NAME        = var.project_name
      STAGE               = var.stage
      DDB_TABLE_NAME      = aws_dynamodb_table.main.name
      RAW_BUCKET          = aws_s3_bucket.raw.bucket
      PROCESSED_BUCKET    = aws_s3_bucket.processed.bucket
      OPENSEARCH_ENDPOINT = aws_opensearch_domain.main.endpoint
      # Pool the invite endpoint creates users in (POST /projects/{id}/invite).
      COGNITO_USER_POOL_ID         = var.cognito_user_pool_id
      EMBEDDING_DIMENSIONS         = tostring(var.embedding_dimensions)
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
  role = aws_iam_role.api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DDBDocumentOps"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem",
          "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:BatchWriteItem",
          # Reads the documents / projects a caller may see in one round-trip.
          "dynamodb:BatchGetItem",
        ]
        Resource = [aws_dynamodb_table.main.arn, "${aws_dynamodb_table.main.arn}/index/*"]
      },
      {
        # PutObject    → sign presigned PUT URLs for GET /documents/upload-url, and
        #                re-write the object in place for POST .../reprocess.
        # GetObject    → sign presigned GET URLs for GET .../file and serve as the
        #                copy source when re-firing the pipeline on reprocess.
        # DeleteObject → remove the original upload on DELETE /documents/{docId}.
        Sid      = "RawBucketReadWrite"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.raw.arn}/*"
      },
      {
        # GetObject    → read processed artefacts (classification/diff/timeline).
        # DeleteObject → purge every processed artefact on document delete.
        Sid      = "ProcessedBucketReadDelete"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.processed.arn}/*"
      },
      {
        # ListBucket → enumerate the document's processed prefix so delete can
        #              remove every artefact under tenants/<tenant>/<docId>/.
        Sid      = "ProcessedBucketList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.processed.arn
      },
      {
        # Similar-clause search (GET/POST) and removing a document's vectors on
        # delete (delete-by-query is a POST).
        Sid      = "OpenSearch"
        Effect   = "Allow"
        Action   = ["es:ESHttpGet", "es:ESHttpPost"]
        Resource = "${aws_opensearch_domain.main.arn}/*"
      },
      {
        # Project invites: create the invited user / look up an existing one.
        # Scoped to the one user pool; no update, delete or password actions.
        Sid      = "CognitoInvite"
        Effect   = "Allow"
        Action   = ["cognito-idp:AdminCreateUser", "cognito-idp:AdminGetUser"]
        Resource = "arn:aws:cognito-idp:${var.aws_region}:${local.account_id}:userpool/${var.cognito_user_pool_id}"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/${local.prefix}-docs-api"
  retention_in_days = var.govern_log_retention_days
}

# HTTP API Gateway (v2) — lightweight, no usage plans needed for v1.
resource "aws_apigatewayv2_api" "documents" {
  name          = "${local.prefix}-docs-api"
  protocol_type = "HTTP"
  cors_configuration {
    # Locked to known frontend origins (was "*"). Add deployed origins via the
    # allowed_origins variable. allow_credentials stays false — the API uses
    # bearer tokens, not cookies. x-tenant-id stays in allow_headers only because
    # the frontend still sends it (dropping it would fail the CORS preflight);
    # the backend ignores the header — the tenant comes from the verified JWT.
    allow_origins = var.allowed_origins
    allow_methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
    allow_headers = ["Content-Type", "Authorization", "x-tenant-id"]
    max_age       = 300
  }
}

# Cognito JWT authorizer — validates the ID token's signature (via the pool's
# JWKS), issuer, and audience (app client id) on every protected route. This is
# the real enforcement layer: requests without a valid token are rejected at the
# gateway before the Lambda runs.
resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.documents.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "${local.prefix}-cognito-jwt"

  jwt_configuration {
    audience = [var.cognito_client_id]
    issuer   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${var.cognito_user_pool_id}"
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

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# Literal route — must be declared before the {docId} wildcard so API GW
# resolves the more-specific path first.
resource "aws_apigatewayv2_route" "get_upload_url" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "GET /documents/upload-url"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "get_document" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "GET /documents/{docId}"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "patch_document" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "PATCH /documents/{docId}"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "delete_document" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "DELETE /documents/{docId}"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "delete_version" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "DELETE /documents/{docId}/versions/{version}"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# Processed-artefact reads — the Lambda router handles these, but the HTTP API
# needs an explicit route per path or the gateway 404s before reaching Lambda.
resource "aws_apigatewayv2_route" "get_classification" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "GET /documents/{docId}/classification"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "get_diff" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "GET /documents/{docId}/diff"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "get_timeline" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "GET /documents/{docId}/timeline"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# Top-KNN similar clauses across the tenant's documents.
resource "aws_apigatewayv2_route" "get_similar" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "GET /documents/{docId}/similar"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# Presigned URL to the original uploaded file (for the split-screen viewer).
resource "aws_apigatewayv2_route" "get_file" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "GET /documents/{docId}/file"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# Re-run the analysis pipeline on the stored upload.
resource "aws_apigatewayv2_route" "post_reprocess" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "POST /documents/{docId}/reprocess"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# Per-tenant project groupings (cloud storage; replaces browser localStorage).
resource "aws_apigatewayv2_route" "get_projects" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "GET /projects"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "save_projects" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "POST /projects"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}


# Per-tenant compliance-pack selection and project membership. The Lambda router
# and the frontend already use these paths, but without a gateway route the HTTP
# API answered 404 before the Lambda ran. All sit behind the same JWT authorizer.

resource "aws_apigatewayv2_route" "get_compliance" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "GET /tenant/compliance"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "save_compliance" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "POST /tenant/compliance"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "invite_member" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "POST /projects/{projectId}/invite"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "remove_member" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "DELETE /projects/{projectId}/members/{email}"
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# ─── Per-project operations, role changes and the playbook ─────────────────────
# Access is per project, by membership (lambdas/shared/access.py). These routes
# let the frontend change ONE project at a time instead of saving the whole
# list; the legacy whole-list `POST /projects` is still served (as a safe merge).

locals {
  access_routes = {
    get_project             = "GET /projects/{projectId}"
    put_project             = "PUT /projects/{projectId}"
    delete_project          = "DELETE /projects/{projectId}"
    add_project_document    = "PUT /projects/{projectId}/documents/{docId}"
    remove_project_document = "DELETE /projects/{projectId}/documents/{docId}"
    set_member_role         = "PATCH /projects/{projectId}/members/{email}"
    get_playbook            = "GET /playbook"
    put_playbook_rule       = "PUT /playbook/rules/{ruleId}"
    delete_playbook_rule    = "DELETE /playbook/rules/{ruleId}"
  }
}

resource "aws_apigatewayv2_route" "access" {
  for_each  = local.access_routes
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.documents.id
  name        = "$default"
  auto_deploy = true

  # Baseline throttling so a single client can't flood the API (defense in
  # depth alongside the JWT authorizer).
  default_route_settings {
    throttling_rate_limit  = 50
    throttling_burst_limit = 100
  }

  # Tighter limits on the routes that cost money or send email per call: each
  # chat is an LLM call, each reprocess is a full pipeline run (several LLM
  # calls), each invite sends an email. These are per-route totals across all
  # callers — HTTP APIs have no per-user throttle.
  route_settings {
    route_key              = aws_apigatewayv2_route.post_chat.route_key
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }
  route_settings {
    route_key              = aws_apigatewayv2_route.post_reprocess.route_key
    throttling_rate_limit  = 2
    throttling_burst_limit = 5
  }
  route_settings {
    route_key              = aws_apigatewayv2_route.get_upload_url.route_key
    throttling_rate_limit  = 10
    throttling_burst_limit = 20
  }
  route_settings {
    route_key              = aws_apigatewayv2_route.invite_member.route_key
    throttling_rate_limit  = 1
    throttling_burst_limit = 3
  }
  # Unauthenticated (HMAC-verified) webhooks, and the per-call matrix rescore.
  route_settings {
    route_key              = aws_apigatewayv2_route.govern_webhooks.route_key
    throttling_rate_limit  = 10
    throttling_burst_limit = 20
  }
  route_settings {
    route_key              = aws_apigatewayv2_route.govern["POST /contracts/{id}/rescore"].route_key
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }

  # One JSON line per request with who called (the verified JWT sub), kept
  # for var.govern_log_retention_days as audit evidence.
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId = "$context.requestId", time = "$context.requestTime", routeKey = "$context.routeKey",
      status    = "$context.status", sub = "$context.authorizer.claims.sub", sourceIp = "$context.identity.sourceIp",
      latencyMs = "$context.responseLatency", integrationError = "$context.integrationErrorMessage"
    })
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
  role             = aws_iam_role.rag.arn
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
      PIPELINE_STAGE          = "08_rag"
      RAG_MAX_CONTEXT_CLAUSES = "8"
      # A retrieval chunk is at most ~1,800 characters; the old 1,200 cap cut the
      # end off every longer clause before the model saw it.
      RAG_MAX_CLAUSE_CHARS     = "2400"
      APPSYNC_GRAPHQL_ENDPOINT = "" # wire in after AppSync API is created
    })
  }

  tracing_config { mode = "Active" }

  depends_on = [aws_cloudwatch_log_group.rag]
}

# ─── RAG over the HTTP API — POST /documents/{docId}/chat ─────────────────────
# Keeps RAG in its own Lambda (separate concern) but exposes it on the same
# documents HTTP API so the frontend Co-pilot can call it directly.
resource "aws_apigatewayv2_integration" "rag_lambda" {
  api_id                 = aws_apigatewayv2_api.documents.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.rag.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "post_chat" {
  api_id    = aws_apigatewayv2_api.documents.id
  route_key = "POST /documents/{docId}/chat"
  target    = "integrations/${aws_apigatewayv2_integration.rag_lambda.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_lambda_permission" "apigw_rag" {
  statement_id  = "AllowAPIGatewayInvokeRAG"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.rag.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.documents.execution_arn}/*/*"
}
