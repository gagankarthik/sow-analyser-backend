# Deletes all blue-iq-sow-dev resources from AWS.
# Run this when you need a clean slate before redeploying.
#
# Usage (from the repo root, after loading .env):
#   Get-Content .env | ForEach-Object { if ($_ -match '^([^#=]+)=(.*)$') { [System.Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim()) } }
#   .\cleanup.ps1

$prefix  = "blue-iq-sow-dev"
$region  = $env:AWS_REGION
$account = (aws sts get-caller-identity --query Account --output text)

if (-not $region)  { Write-Error "AWS_REGION not set"; exit 1 }
if (-not $account) { Write-Error "Cannot resolve AWS account ID - check credentials"; exit 1 }

Write-Host "Cleaning up prefix=$prefix  region=$region  account=$account"
Write-Host ""

function Try-Delete($label, $cmd) {
    Write-Host "  $label ... " -NoNewline
    try {
        Invoke-Expression $cmd 2>$null | Out-Null
        Write-Host "done"
    } catch {
        Write-Host "skipped (not found or already gone)"
    }
}

# ── Lambda functions ─────────────────────────────────────────────────────────
Write-Host "Lambda functions:"
foreach ($fn in @(
    "$prefix-pipeline", "$prefix-api", "$prefix-rag",
    "$prefix-01-parse", "$prefix-02-classify", "$prefix-03-embed",
    "$prefix-04-graph", "$prefix-05-diff", "$prefix-06-timeline", "$prefix-07-persist"
)) {
    Try-Delete $fn "aws lambda delete-function --function-name $fn --region $region"
}

# ── Lambda layer versions ────────────────────────────────────────────────────
Write-Host "Lambda layers:"
$layers = aws lambda list-layers --region $region --query "Layers[?starts_with(LayerName,'$prefix')].LayerName" --output text 2>$null
foreach ($l in ($layers -split '\s+')) {
    if (-not $l) { continue }
    $versions = aws lambda list-layer-versions --layer-name $l --region $region --query "LayerVersions[].Version" --output text 2>$null
    foreach ($v in ($versions -split '\s+')) {
        if (-not $v) { continue }
        Try-Delete "$l v$v" "aws lambda delete-layer-version --layer-name $l --version-number $v --region $region"
    }
}

# ── Step Functions ───────────────────────────────────────────────────────────
Write-Host "Step Functions:"
Try-Delete "$prefix-pipeline" "aws stepfunctions delete-state-machine --state-machine-arn arn:aws:states:${region}:${account}:stateMachine:${prefix}-pipeline"

# ── EventBridge ──────────────────────────────────────────────────────────────
Write-Host "EventBridge:"
aws events remove-targets --rule "$prefix-raw-object-created" --ids Id1 --region $region 2>$null | Out-Null
Try-Delete "$prefix-raw-object-created" "aws events delete-rule --name $prefix-raw-object-created --region $region"

# ── API Gateway (HTTP API v2) ────────────────────────────────────────────────
Write-Host "API Gateway:"
$apis = aws apigatewayv2 get-apis --region $region --query "Items[?Name=='$prefix-docs-api'].ApiId" --output text 2>$null
foreach ($id in ($apis -split '\s+')) {
    if (-not $id) { continue }
    Try-Delete "$prefix-docs-api ($id)" "aws apigatewayv2 delete-api --api-id $id --region $region"
}

# ── IAM Roles ────────────────────────────────────────────────────────────────
Write-Host "IAM Roles:"
foreach ($roleName in @("$prefix-pipeline-base", "$prefix-sfn", "$prefix-events-to-sfn")) {
    # Detach managed policies
    $managed = aws iam list-attached-role-policies --role-name $roleName --query "AttachedPolicies[].PolicyArn" --output text 2>$null
    foreach ($p in ($managed -split '\s+')) {
        if ($p) { aws iam detach-role-policy --role-name $roleName --policy-arn $p 2>$null | Out-Null }
    }
    # Delete inline policies
    $inline = aws iam list-role-policies --role-name $roleName --query "PolicyNames" --output text 2>$null
    foreach ($p in ($inline -split '\s+')) {
        if ($p) { aws iam delete-role-policy --role-name $roleName --policy-name $p 2>$null | Out-Null }
    }
    Try-Delete $roleName "aws iam delete-role --role-name $roleName"
}

# ── CloudWatch Log Groups ────────────────────────────────────────────────────
Write-Host "CloudWatch Log Groups:"
foreach ($lg in @(
    "/aws/lambda/$prefix-pipeline",
    "/aws/lambda/$prefix-api",
    "/aws/lambda/$prefix-rag",
    "/aws/lambda/$prefix-01-parse",
    "/aws/lambda/$prefix-02-classify",
    "/aws/lambda/$prefix-03-embed",
    "/aws/lambda/$prefix-04-graph",
    "/aws/lambda/$prefix-05-diff",
    "/aws/lambda/$prefix-06-timeline",
    "/aws/lambda/$prefix-07-persist",
    "/aws/vendedlogs/states/$prefix-pipeline"
)) {
    Try-Delete $lg "aws logs delete-log-group --log-group-name '$lg' --region $region"
}

# ── SQS ──────────────────────────────────────────────────────────────────────
Write-Host "SQS:"
$queueUrl = aws sqs get-queue-url --queue-name "$prefix-pipeline-dlq" --region $region --query QueueUrl --output text 2>$null
if ($queueUrl) {
    Try-Delete "$prefix-pipeline-dlq" "aws sqs delete-queue --queue-url $queueUrl --region $region"
}

# ── DynamoDB ─────────────────────────────────────────────────────────────────
Write-Host "DynamoDB:"
Try-Delete "$prefix-main" "aws dynamodb delete-table --table-name $prefix-main --region $region"

# ── S3 Buckets ───────────────────────────────────────────────────────────────
Write-Host "S3 Buckets:"
foreach ($bucket in @("$prefix-raw-$account", "$prefix-processed-$account")) {
    Write-Host "  $bucket ... " -NoNewline
    try {
        # Delete all object versions (required when versioning is enabled)
        $versions = aws s3api list-object-versions --bucket $bucket 2>$null | ConvertFrom-Json
        if ($versions.Versions) {
            $versions.Versions | ForEach-Object {
                aws s3api delete-object --bucket $bucket --key $_.Key --version-id $_.VersionId 2>$null | Out-Null
            }
        }
        if ($versions.DeleteMarkers) {
            $versions.DeleteMarkers | ForEach-Object {
                aws s3api delete-object --bucket $bucket --key $_.Key --version-id $_.VersionId 2>$null | Out-Null
            }
        }
        aws s3 rm "s3://$bucket" --recursive --region $region 2>$null | Out-Null
        aws s3api delete-bucket --bucket $bucket --region $region 2>$null | Out-Null
        Write-Host "done"
    } catch {
        Write-Host "skipped"
    }
}

# ── OpenSearch ────────────────────────────────────────────────────────────────
Write-Host "OpenSearch:"
Try-Delete "$prefix-search" "aws opensearch delete-domain --domain-name $prefix-search --region $region"

Write-Host ""
Write-Host "Cleanup complete. You can now run a fresh deploy."
