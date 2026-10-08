# ─── Base Lambda execution role (common trust + CloudWatch/X-Ray) ───────────────

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "pipeline_base" {
  name               = "${local.prefix}-pipeline-base"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy_attachment" "pipeline_base_logs" {
  role       = aws_iam_role.pipeline_base.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "pipeline_base_xray" {
  role       = aws_iam_role.pipeline_base.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "pipeline_base_dlq" {
  name = "dlq-send"
  role = aws_iam_role.pipeline_base.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:SendMessage"]
      Resource = aws_sqs_queue.pipeline_dlq.arn
    }]
  })
}


# ─── Stage-specific policies (attached to the shared base role) ────────────────

resource "aws_iam_role_policy" "parse" {
  name = "parse"
  role = aws_iam_role.pipeline_base.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadRaw"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:HeadObject"]
        Resource = "${aws_s3_bucket.raw.arn}/*"
      },
      {
        Sid      = "WriteProcessed"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.processed.arn}/*"
      },
      {
        Sid      = "Textract"
        Effect   = "Allow"
        Action   = ["textract:StartDocumentTextDetection", "textract:GetDocumentTextDetection"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "classify" {
  name = "classify"
  role = aws_iam_role.pipeline_base.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadProcessed"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.processed.arn}/*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "embed" {
  name = "embed"
  role = aws_iam_role.pipeline_base.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadProcessed"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.processed.arn}/*"
      },
      {
        Sid      = "OpenSearch"
        Effect   = "Allow"
        Action   = ["es:ESHttpGet", "es:ESHttpPost", "es:ESHttpPut", "es:ESHttpDelete", "es:ESHttpHead"]
        Resource = "${aws_opensearch_domain.main.arn}/*"
      },
      {
        Sid      = "DDBEmbedCache"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.main.arn
      },
    ]
  })
}

resource "aws_iam_role_policy" "graph" {
  name = "graph"
  role = aws_iam_role.pipeline_base.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DDBReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem",
          "dynamodb:UpdateItem", "dynamodb:Query",
          "dynamodb:BatchGetItem",
        ]
        Resource = [aws_dynamodb_table.main.arn, "${aws_dynamodb_table.main.arn}/index/*"]
      },
      {
        Sid      = "OpenSearchRead"
        Effect   = "Allow"
        Action   = ["es:ESHttpGet", "es:ESHttpPost"]
        Resource = "${aws_opensearch_domain.main.arn}/*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "diff" {
  name = "diff"
  role = aws_iam_role.pipeline_base.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadWriteProcessed"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.processed.arn}/*"
      },
      {
        Sid      = "DDBRead"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query"]
        Resource = [aws_dynamodb_table.main.arn, "${aws_dynamodb_table.main.arn}/index/*"]
      },
    ]
  })
}

resource "aws_iam_role_policy" "timeline" {
  name = "timeline"
  role = aws_iam_role.pipeline_base.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadWriteProcessed"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.processed.arn}/*"
      },
      {
        Sid    = "DDBReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem",
          "dynamodb:UpdateItem", "dynamodb:Query",
        ]
        Resource = [aws_dynamodb_table.main.arn, "${aws_dynamodb_table.main.arn}/index/*"]
      },
    ]
  })
}

resource "aws_iam_role_policy" "persist" {
  name = "persist"
  role = aws_iam_role.pipeline_base.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteProcessed"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.processed.arn}/*"
      },
      {
        Sid    = "DDBWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem",
          "dynamodb:UpdateItem", "dynamodb:Query",
        ]
        Resource = [aws_dynamodb_table.main.arn, "${aws_dynamodb_table.main.arn}/index/*"]
      },
    ]
  })
}

# Inter-stage state: the pipeline parks bulky stage output in the processed
# bucket (Step Functions caps state at 256 KB) and removes it when the run ends.
# It also discards a run's artefacts when the document was deleted mid-run.
resource "aws_iam_role_policy" "pipeline_state" {
  name = "pipeline-state"
  role = aws_iam_role.pipeline_base.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ProcessedStateObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.processed.arn}/*"
      },
      {
        Sid      = "ProcessedList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.processed.arn
      },
    ]
  })
}


# ─── API Lambda role ───────────────────────────────────────────────────────────
# The API and RAG Lambdas used to run under the pipeline role, so the
# internet-facing functions also held every pipeline permission (Textract, all
# stage writes) and vice versa. Each function now has its own role.

resource "aws_iam_role" "api" {
  name               = "${local.prefix}-api"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy_attachment" "api_logs" {
  role       = aws_iam_role.api.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "api_xray" {
  role       = aws_iam_role.api.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}


# ─── RAG Lambda role ───────────────────────────────────────────────────────────

resource "aws_iam_role" "rag" {
  name               = "${local.prefix}-rag"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy_attachment" "rag_logs" {
  role       = aws_iam_role.rag.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "rag_xray" {
  role       = aws_iam_role.rag.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "rag" {
  name = "rag"
  role = aws_iam_role.rag.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "OpenSearch"
        Effect   = "Allow"
        Action   = ["es:ESHttpGet", "es:ESHttpPost"]
        Resource = "${aws_opensearch_domain.main.arn}/*"
      },
      {
        # Read-only: the document's META row, the caller's project memberships
        # (GSI1) and the projects' document lists — to work out which documents
        # the caller may read BEFORE anything is searched or answered.
        Sid      = "DDBReadAccess"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:BatchGetItem"]
        Resource = [aws_dynamodb_table.main.arn, "${aws_dynamodb_table.main.arn}/index/*"]
      },
      {
        # Read-only: the Govern contract, its review and blockers, so Sonar can
        # answer "what do I need to change" from the organization's matrix.
        Sid      = "GovernContractsRead"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query"]
        Resource = [aws_dynamodb_table.govern_contracts.arn]
      },
      {
        Sid      = "GovernReviewsRead"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${aws_s3_bucket.processed.arn}/*"]
      },
      {
        Sid      = "AppSync"
        Effect   = "Allow"
        Action   = ["appsync:GraphQL"]
        Resource = "arn:aws:appsync:${local.region}:${local.account_id}:apis/*"
      },
    ]
  })
}


# ─── Step Functions execution role ─────────────────────────────────────────────

data "aws_iam_policy_document" "sfn_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sfn" {
  name               = "${local.prefix}-sfn"
  assume_role_policy = data.aws_iam_policy_document.sfn_trust.json
}

resource "aws_iam_role_policy" "sfn" {
  name = "sfn-policy"
  role = aws_iam_role.sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokePipeline"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [aws_lambda_function.pipeline.arn]
      },
      {
        Sid      = "DDBMarkFailed"
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.main.arn
      },
      {
        # "Document Analysed" → the platform bus (govern-intake subscribes).
        Sid      = "PutEvents"
        Effect   = "Allow"
        Action   = ["events:PutEvents"]
        Resource = aws_cloudwatch_event_bus.platform.arn
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery", "logs:ListLogDeliveries",
          "logs:PutResourcePolicy", "logs:DescribeResourcePolicies",
          "logs:DescribeLogGroups",
        ]
        Resource = "*"
      },
      {
        Sid      = "XRay"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
        Resource = "*"
      },
    ]
  })
}


# ─── EventBridge → Step Functions role ─────────────────────────────────────────

data "aws_iam_policy_document" "events_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "events_to_sfn" {
  name               = "${local.prefix}-events-to-sfn"
  assume_role_policy = data.aws_iam_policy_document.events_trust.json
}

resource "aws_iam_role_policy" "events_to_sfn" {
  name = "start-execution"
  role = aws_iam_role.events_to_sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["states:StartExecution"]
      Resource = aws_sfn_state_machine.pipeline.arn
    }]
  })
}


# ─── OpenAI key from Secrets Manager (pipeline + RAG) ─────────────────────────

resource "aws_iam_role_policy" "openai_secret" {
  for_each = { pipeline = aws_iam_role.pipeline_base.id, rag = aws_iam_role.rag.id }
  name     = "openai-secret"
  role     = each.value
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "ReadOpenAIKey", Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.openai.arn },
      { Sid = "DecryptOpenAIKey", Effect = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.govern.arn },
    ]
  })
}
