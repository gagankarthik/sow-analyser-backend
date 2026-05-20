# ─── CloudWatch log group for Step Functions ───────────────────────────────────

resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/vendedlogs/states/${local.prefix}-pipeline"
  retention_in_days = var.log_retention_days
}


# ─── Express state machine ─────────────────────────────────────────────────────
# Runs 7 stages in series.  Any failure catches → MarkFailed (DDB update).
# On success: emit blue-iq.documentReady event on the default EventBridge bus.

locals {
  sfn_definition = jsonencode({
    Comment = "Blue-IQ document ingestion pipeline"
    StartAt = "Parse"
    States = {
      Parse = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline["01-parse"].arn
          "Payload.$"  = "$"
        }
        OutputPath = "$.Payload"
        Catch = [{ ErrorEquals = ["States.ALL"], Next = "MarkFailed", ResultPath = "$.error" }]
        Next = "Classify"
      }
      Classify = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline["02-classify"].arn
          "Payload.$"  = "$"
        }
        OutputPath = "$.Payload"
        Catch = [{ ErrorEquals = ["States.ALL"], Next = "MarkFailed", ResultPath = "$.error" }]
        Next = "Embed"
      }
      Embed = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline["03-embed"].arn
          "Payload.$"  = "$"
        }
        OutputPath = "$.Payload"
        Catch = [{ ErrorEquals = ["States.ALL"], Next = "MarkFailed", ResultPath = "$.error" }]
        Next = "Graph"
      }
      Graph = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline["04-graph"].arn
          "Payload.$"  = "$"
        }
        OutputPath = "$.Payload"
        Catch = [{ ErrorEquals = ["States.ALL"], Next = "MarkFailed", ResultPath = "$.error" }]
        Next = "Diff"
      }
      Diff = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline["05-diff"].arn
          "Payload.$"  = "$"
        }
        OutputPath = "$.Payload"
        Catch = [{ ErrorEquals = ["States.ALL"], Next = "MarkFailed", ResultPath = "$.error" }]
        Next = "Timeline"
      }
      Timeline = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline["06-timeline"].arn
          "Payload.$"  = "$"
        }
        OutputPath = "$.Payload"
        Catch = [{ ErrorEquals = ["States.ALL"], Next = "MarkFailed", ResultPath = "$.error" }]
        Next = "Persist"
      }
      Persist = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline["07-persist"].arn
          "Payload.$"  = "$"
        }
        OutputPath = "$.Payload"
        Catch = [{ ErrorEquals = ["States.ALL"], Next = "MarkFailed", ResultPath = "$.error" }]
        Next = "DocumentReady"
      }
      DocumentReady = {
        Type     = "Task"
        Resource = "arn:aws:states:::events:putEvents"
        Parameters = {
          Entries = [{
            "Detail.$"   = "$"
            DetailType   = "blue-iq.documentReady"
            Source       = "blue-iq.${var.stage}.pipeline"
            EventBusName = "default"
          }]
        }
        End = true
      }
      MarkFailed = {
        Type     = "Task"
        Resource = "arn:aws:states:::dynamodb:updateItem"
        Parameters = {
          TableName = aws_dynamodb_table.main.name
          Key = {
            PK = { "S.$" = "States.Format('DOC#{}', $.docId)" }
            SK = { S = "META" }
          }
          UpdateExpression = "SET #st = :failed, updatedAt = :ts"
          ExpressionAttributeNames = { "#st" = "status" }
          ExpressionAttributeValues = {
            ":failed" = { S = "FAILED" }
            ":ts"     = { "S.$" = "$$.State.EnteredTime" }
          }
        }
        End = true
      }
    }
  })
}

resource "aws_sfn_state_machine" "pipeline" {
  name       = "${local.prefix}-pipeline"
  type       = "EXPRESS"
  role_arn   = aws_iam_role.sfn.arn
  definition = local.sfn_definition

  tracing_configuration { enabled = true }

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  tags = { Name = "${local.prefix}-pipeline" }
}


# ─── EventBridge rule: S3 ObjectCreated → pipeline ─────────────────────────────

resource "aws_cloudwatch_event_rule" "raw_object_created" {
  name        = "${local.prefix}-raw-object-created"
  description = "Trigger Blue-IQ pipeline when a file lands in the raw S3 bucket"

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = { name = [aws_s3_bucket.raw.bucket] }
    }
  })
}

resource "aws_cloudwatch_event_target" "raw_to_sfn" {
  rule     = aws_cloudwatch_event_rule.raw_object_created.name
  arn      = aws_sfn_state_machine.pipeline.arn
  role_arn = aws_iam_role.events_to_sfn.arn

  # Remap S3 event fields into the PipelineEvent shape the parse stage expects.
  input_transformer {
    input_paths = {
      bucket = "$.detail.bucket.name"
      key    = "$.detail.object.key"
    }
    # docId and tenantId are extracted by the parse Lambda from the S3 key.
    # Key format: tenants/<tenantId>/uploads/<docId>/<filename>
    input_template = jsonencode({
      rawBucket        = "<bucket>"
      rawKey           = "<key>"
      processedBucket  = aws_s3_bucket.processed.bucket
      docId            = "<key>"
      tenantId         = "unknown"
    })
  }
}
