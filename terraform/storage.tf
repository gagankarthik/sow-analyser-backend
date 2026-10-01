# ─── S3 ────────────────────────────────────────────────────────────────────────

# force_destroy lets `terraform destroy` wipe a bucket that still holds objects.
# Convenient for dev/staging; in prod it would silently delete every customer
# contract, so it is off there.
resource "aws_s3_bucket" "raw" {
  bucket        = "${local.prefix}-raw-${local.account_id}"
  force_destroy = var.stage != "prod"
}

resource "aws_s3_bucket" "processed" {
  bucket        = "${local.prefix}-processed-${local.account_id}"
  force_destroy = var.stage != "prod"
}

resource "aws_s3_bucket_versioning" "raw" {
  bucket = aws_s3_bucket.raw.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "processed" {
  bucket = aws_s3_bucket.processed.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "raw" {
  bucket                  = aws_s3_bucket.raw.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "processed" {
  bucket                  = aws_s3_bucket.processed.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Refuse any request that is not over TLS (both buckets hold contract text).
resource "aws_s3_bucket_policy" "raw_tls_only" {
  bucket     = aws_s3_bucket.raw.id
  depends_on = [aws_s3_bucket_public_access_block.raw]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.raw.arn, "${aws_s3_bucket.raw.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_s3_bucket_policy" "processed_tls_only" {
  bucket     = aws_s3_bucket.processed.id
  depends_on = [aws_s3_bucket_public_access_block.processed]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.processed.arn, "${aws_s3_bucket.processed.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# The raw bucket is versioned, so deleting a document only adds a delete marker:
# the contract itself stays as a "noncurrent version" forever. Expire those so a
# deleted (or re-analysed) upload is really gone after 30 days.
resource "aws_s3_bucket_lifecycle_configuration" "raw" {
  bucket     = aws_s3_bucket.raw.id
  depends_on = [aws_s3_bucket_versioning.raw]

  rule {
    id     = "expire-noncurrent-uploads"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  rule {
    id     = "remove-orphan-delete-markers"
    status = "Enabled"
    filter {}
    expiration {
      expired_object_delete_marker = true
    }
  }

  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# EventBridge notifications so S3 ObjectCreated → EventBridge → Step Functions.
resource "aws_s3_bucket_notification" "raw" {
  bucket      = aws_s3_bucket.raw.id
  eventbridge = true
}

# CORS for the raw bucket. The browser uploads files DIRECTLY to S3 with the
# presigned PUT URL from GET /documents/upload-url, so S3 itself must allow the
# frontend origin and the preflight (OPTIONS) — otherwise the upload fails with
# "No 'Access-Control-Allow-Origin' header". Origins are shared with the API
# CORS via var.allowed_origins.
resource "aws_s3_bucket_cors_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  cors_rule {
    allowed_origins = var.allowed_origins
    allowed_methods = ["PUT", "GET", "HEAD"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}


# ─── DynamoDB ──────────────────────────────────────────────────────────────────
#
# Single-table design.  Key schema:
#   PK = DOC#<docId>     SK = META | V#NNNNNN | CHG#<id> | LINK#<parent> | CHILD#<child>
#   PK = CACHE#<sha256>  SK = EMBEDDING
#   GSI1: GSI1PK (TENANT#<id>) / GSI1SK (DOC#<docId>)   — tenant → documents

resource "aws_dynamodb_table" "main" {
  name         = "${local.prefix}-main"
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
  attribute {
    name = "GSI1PK"
    type = "S"
  }
  attribute {
    name = "GSI1SK"
    type = "S"
  }

  global_secondary_index {
    name            = "GSI1"
    hash_key        = "GSI1PK"
    range_key       = "GSI1SK"
    projection_type = "ALL"
  }

  point_in_time_recovery { enabled = true }

  # Block an accidental table delete (console, CLI or `terraform destroy`) in prod.
  deletion_protection_enabled = var.stage == "prod"

  server_side_encryption { enabled = true }

  tags = { Name = "${local.prefix}-main" }
}


# ─── SQS DLQ ───────────────────────────────────────────────────────────────────

resource "aws_sqs_queue" "pipeline_dlq" {
  name                       = "${local.prefix}-pipeline-dlq"
  message_retention_seconds  = 1209600 # 14 days
  visibility_timeout_seconds = 300
  sqs_managed_sse_enabled    = true
}

resource "aws_sqs_queue_policy" "pipeline_dlq_ssl" {
  queue_url = aws_sqs_queue.pipeline_dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyNonSSL"
      Effect    = "Deny"
      Principal = "*"
      Action    = "sqs:*"
      Resource  = aws_sqs_queue.pipeline_dlq.arn
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}


# ─── OpenSearch ────────────────────────────────────────────────────────────────

resource "aws_opensearch_domain" "main" {
  domain_name    = "${local.prefix}-search"
  engine_version = "OpenSearch_2.11"

  cluster_config {
    instance_type  = var.opensearch_instance_type
    instance_count = 1
  }

  ebs_options {
    ebs_enabled = true
    volume_type = "gp3"
    volume_size = var.opensearch_volume_gb
  }

  encrypt_at_rest { enabled = true }
  node_to_node_encryption { enabled = true }

  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-2019-07"
  }

  access_policies = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { AWS = [
        aws_iam_role.pipeline_base.arn,
        aws_iam_role.api.arn,
        aws_iam_role.rag.arn,
      ] }
      # Data-plane HTTP only (was es:*, which also allowed reconfiguring or
      # deleting the domain). Each role's own IAM policy narrows this further.
      Action    = "es:ESHttp*"
      Resource  = "arn:aws:es:${local.region}:${local.account_id}:domain/${local.prefix}-search/*"
    }]
  })

  tags = { Name = "${local.prefix}-search" }
}
