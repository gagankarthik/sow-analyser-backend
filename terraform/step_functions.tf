# ─── CloudWatch log group for Step Functions ───────────────────────────────────

resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/vendedlogs/states/${local.prefix}-pipeline"
  retention_in_days = var.log_retention_days
}


# ─── Express state machine ─────────────────────────────────────────────────────
# Single Lambda handles all seven stages.  Each state injects _stage into the
# payload via States.JsonMerge; the handler pops it, dispatches, and returns the
# clean pipeline event for the next stage.

locals {
  _fn  = aws_lambda_function.pipeline.arn
  _catch = [{ ErrorEquals = ["States.ALL"], Next = "MarkFailed", ResultPath = "$.error" }]

  sfn_definition = jsonencode({
    Comment = "Blue-IQ document ingestion pipeline"
    StartAt = "Parse"
    States = {
      Parse = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local._fn
          "Payload.$"  = "States.JsonMerge($, States.StringToJson('{\"_stage\":\"01_parse\"}'), false)"
        }
        OutputPath = "$.Payload"
        Catch      = local._catch
        Next       = "Classify"
      }
      Classify = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local._fn
          "Payload.$"  = "States.JsonMerge($, States.StringToJson('{\"_stage\":\"02_classify\"}'), false)"
        }
        OutputPath = "$.Payload"
        Catch      = local._catch
        Next       = "Embed"
      }
      Embed = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local._fn
          "Payload.$"  = "States.JsonMerge($, States.StringToJson('{\"_stage\":\"03_embed\"}'), false)"
        }
        OutputPath = "$.Payload"
        Catch      = local._catch
        Next       = "Graph"
      }
      Graph = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local._fn
          "Payload.$"  = "States.JsonMerge($, States.StringToJson('{\"_stage\":\"04_graph\"}'), false)"
        }
        OutputPath = "$.Payload"
        Catch      = local._catch
        Next       = "Diff"
      }
      Diff = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local._fn
          "Payload.$"  = "States.JsonMerge($, States.StringToJson('{\"_stage\":\"05_diff\"}'), false)"
        }
        OutputPath = "$.Payload"
        Catch      = local._catch
        Next       = "Timeline"
      }
      Timeline = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local._fn
          "Payload.$"  = "States.JsonMerge($, States.StringToJson('{\"_stage\":\"06_timeline\"}'), false)"
        }
        OutputPath = "$.Payload"
        Catch      = local._catch
        Next       = "Persist"
      }
      Persist = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local._fn
          "Payload.$"  = "States.JsonMerge($, States.StringToJson('{\"_stage\":\"07_persist\"}'), false)"
        }
        OutputPath = "$.Payload"
        Catch      = local._catch
        Next       = "DocumentReady"
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
            # Derive the REAL docId from the raw S3 key
            # (tenants/<tenantId>/uploads/<docId>/<file>) rather than $.docId.
            # On a Parse failure $.docId is still the full S3 key, so the old
            # version updated a non-existent row and the document stayed stuck
            # on "processing" forever. rawKey is always present in the event.
            PK = { "S.$" = "States.Format('DOC#{}', States.ArrayGetItem(States.StringSplit($.rawKey, '/'), 3))" }
            SK = { S = "META" }
          }
          UpdateExpression          = "SET #st = :failed, updatedAt = :ts, errorMessage = :err"
          ExpressionAttributeNames  = { "#st" = "status" }
          ExpressionAttributeValues = {
            ":failed" = { S = "FAILED" }
            ":ts"     = { "S.$" = "$$.State.EnteredTime" }
            ":err"    = { "S.$" = "States.Format('{}: {}', $.error.Error, $.error.Cause)" }
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
    source        = ["aws.s3"]
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

  input_transformer {
    input_paths = {
      bucket = "$.detail.bucket.name"
      key    = "$.detail.object.key"
    }
    # IMPORTANT: Do NOT use jsonencode() here. Terraform's jsonencode() HTML-escapes
    # < and > as < / >, which makes EventBridge unable to recognise the
    # <bucket> and <key> placeholders — they arrive at the Lambda as the literal
    # strings "<bucket>" and "<key>". Use a plain string so the angle brackets are
    # preserved verbatim for EventBridge substitution.
    # docId and tenantId are extracted from the S3 key by the parse stage.
    # Key format: tenants/<tenantId>/uploads/<docId>/<filename>
    input_template = "{\"rawBucket\":\"<bucket>\",\"rawKey\":\"<key>\",\"processedBucket\":\"${aws_s3_bucket.processed.bucket}\",\"docId\":\"<key>\",\"tenantId\":\"unknown\"}"
  }
}
