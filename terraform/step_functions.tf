# ─── CloudWatch log group for Step Functions ───────────────────────────────────

resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/vendedlogs/states/${local.prefix}-pipeline"
  retention_in_days = var.log_retention_days
}


# ─── Standard state machine ────────────────────────────────────────────────────
# Single Lambda handles all seven stages.  Each state injects _stage into the
# payload via States.JsonMerge; the handler pops it, dispatches, and returns the
# clean pipeline event for the next stage.
#
# STANDARD, not EXPRESS: an Express execution is killed at 5 minutes total, but a
# single stage may legitimately run up to the Lambda's 600 s (Textract OCR, a long
# extraction + validation pass). When Express hit its cap the execution just
# stopped — the Catch never ran, MarkFailed never ran, and the document stayed on
# "processing" forever. Standard has no such cap, runs each state exactly once
# (Express is at-least-once, i.e. duplicate LLM spend), and costs ~$0.0002/doc.

locals {
  _fn    = aws_lambda_function.pipeline.arn
  _catch = [{ ErrorEquals = ["States.ALL"], Next = "MarkFailed", ResultPath = "$.error" }]

  # Slightly above the Lambda timeout, so a hung invoke is caught (States.Timeout
  # → MarkFailed) instead of leaving the execution open.
  _task_timeout = aws_lambda_function.pipeline.timeout + 30

  # Retry only transient Lambda-service faults. A stage's own error is NOT
  # retried here — that would repeat the LLM calls it already paid for.
  _retry = [{
    ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"]
    IntervalSeconds = 2
    MaxAttempts     = 3
    BackoffRate     = 2
  }]

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
        OutputPath     = "$.Payload"
        TimeoutSeconds = local._task_timeout
        Retry          = local._retry
        Catch          = local._catch
        Next           = "Classify"
      }
      Classify = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local._fn
          "Payload.$"  = "States.JsonMerge($, States.StringToJson('{\"_stage\":\"02_classify\"}'), false)"
        }
        OutputPath     = "$.Payload"
        TimeoutSeconds = local._task_timeout
        Retry          = local._retry
        Catch          = local._catch
        Next           = "Embed"
      }
      Embed = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local._fn
          "Payload.$"  = "States.JsonMerge($, States.StringToJson('{\"_stage\":\"03_embed\"}'), false)"
        }
        OutputPath     = "$.Payload"
        TimeoutSeconds = local._task_timeout
        Retry          = local._retry
        Catch          = local._catch
        Next           = "Graph"
      }
      Graph = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local._fn
          "Payload.$"  = "States.JsonMerge($, States.StringToJson('{\"_stage\":\"04_graph\"}'), false)"
        }
        OutputPath     = "$.Payload"
        TimeoutSeconds = local._task_timeout
        Retry          = local._retry
        Catch          = local._catch
        Next           = "Diff"
      }
      Diff = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local._fn
          "Payload.$"  = "States.JsonMerge($, States.StringToJson('{\"_stage\":\"05_diff\"}'), false)"
        }
        OutputPath     = "$.Payload"
        TimeoutSeconds = local._task_timeout
        Retry          = local._retry
        Catch          = local._catch
        Next           = "Timeline"
      }
      Timeline = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local._fn
          "Payload.$"  = "States.JsonMerge($, States.StringToJson('{\"_stage\":\"06_timeline\"}'), false)"
        }
        OutputPath     = "$.Payload"
        TimeoutSeconds = local._task_timeout
        Retry          = local._retry
        Catch          = local._catch
        Next           = "Persist"
      }
      Persist = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local._fn
          "Payload.$"  = "States.JsonMerge($, States.StringToJson('{\"_stage\":\"07_persist\"}'), false)"
        }
        OutputPath     = "$.Payload"
        TimeoutSeconds = local._task_timeout
        Retry          = local._retry
        Catch          = local._catch
        Next           = "PublishDocumentAnalysed"
      }
      # Tell the platform bus the document is analysed (govern-intake turns it
      # into a contract). Retried; a failure here must NOT fail an analysis that
      # already succeeded: the hourly Govern reconciliation captures any
      # document whose event was lost.
      PublishDocumentAnalysed = {
        Type     = "Task"
        Resource = "arn:aws:states:::events:putEvents"
        Parameters = {
          Entries = [{
            "Detail.$"   = "$"
            DetailType   = "Document Analysed"
            Source       = "blue-iq.pipeline"
            EventBusName = aws_cloudwatch_event_bus.platform.name
          }]
        }
        ResultPath = null
        Retry = [{
          ErrorEquals     = ["States.ALL"]
          IntervalSeconds = 2
          MaxAttempts     = 4
          BackoffRate     = 2
        }]
        Catch = [{ ErrorEquals = ["States.ALL"], Next = "AnalysedEventNotPublished", ResultPath = "$.publishError" }]
        End   = true
      }
      AnalysedEventNotPublished = {
        Type    = "Succeed"
        Comment = "The document is READY; Govern reconciliation will capture it."
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
          UpdateExpression = "SET #st = :failed, updatedAt = :ts, errorMessage = :err"
          # updateItem upserts: without this, a failure for a document that was
          # deleted mid-run would recreate a tenant-less ghost row.
          ConditionExpression      = "attribute_exists(PK)"
          ExpressionAttributeNames = { "#st" = "status" }
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
  # Renamed from "-pipeline": changing the type replaces the state machine, and
  # re-creating one under the name of a machine that is still being deleted fails.
  name       = "${local.prefix}-ingest"
  type       = "STANDARD"
  role_arn   = aws_iam_role.sfn.arn
  definition = local.sfn_definition

  tracing_configuration { enabled = true }

  logging_configuration {
    log_destination = "${aws_cloudwatch_log_group.sfn.arn}:*"
    # Errors only, and never the state input/output: execution data is the
    # pipeline payload, which must not be copied into CloudWatch.
    include_execution_data = false
    level                  = "ERROR"
  }

  tags = { Name = "${local.prefix}-ingest" }
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
