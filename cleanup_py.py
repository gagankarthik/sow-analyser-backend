"""Delete all blue-iq-sow-dev resources from AWS using boto3."""
import boto3, time, sys

PREFIX  = "blue-iq-sow-dev"
REGION  = "us-east-2"

session  = boto3.Session(region_name=REGION)
account  = session.client("sts").get_caller_identity()["Account"]

print(f"Cleaning up  prefix={PREFIX}  region={REGION}  account={account}\n")

def ok(label):
    print(f"  {label} ... done")

def skip(label, e):
    print(f"  {label} ... skipped ({type(e).__name__})")


# ── Lambda functions ──────────────────────────────────────────────────────────
lam = session.client("lambda")
fn_names = [
    f"{PREFIX}-pipeline", f"{PREFIX}-api", f"{PREFIX}-rag",
    f"{PREFIX}-01-parse", f"{PREFIX}-02-classify", f"{PREFIX}-03-embed",
    f"{PREFIX}-04-graph",  f"{PREFIX}-05-diff",    f"{PREFIX}-06-timeline",
    f"{PREFIX}-07-persist",
]
print("Lambda functions:")
for fn in fn_names:
    try:
        lam.delete_function(FunctionName=fn)
        ok(fn)
    except lam.exceptions.ResourceNotFoundException as e:
        skip(fn, e)
    except Exception as e:
        skip(fn, e)

# ── Lambda layers ─────────────────────────────────────────────────────────────
print("Lambda layers:")
try:
    paginator = lam.get_paginator("list_layers")
    for page in paginator.paginate():
        for layer in page["Layers"]:
            if layer["LayerName"].startswith(PREFIX):
                vers = lam.list_layer_versions(LayerName=layer["LayerName"])["LayerVersions"]
                for v in vers:
                    try:
                        lam.delete_layer_version(LayerName=layer["LayerName"], VersionNumber=v["Version"])
                        ok(f"{layer['LayerName']} v{v['Version']}")
                    except Exception as e:
                        skip(f"{layer['LayerName']} v{v['Version']}", e)
except Exception as e:
    print(f"  layer listing failed: {e}")

# ── Step Functions ────────────────────────────────────────────────────────────
print("Step Functions:")
sfn = session.client("stepfunctions")
arn = f"arn:aws:states:{REGION}:{account}:stateMachine:{PREFIX}-pipeline"
try:
    sfn.delete_state_machine(stateMachineArn=arn)
    ok(f"{PREFIX}-pipeline")
except Exception as e:
    skip(f"{PREFIX}-pipeline", e)

# ── EventBridge ───────────────────────────────────────────────────────────────
print("EventBridge:")
eb = session.client("events")
rule_name = f"{PREFIX}-raw-object-created"
try:
    targets = eb.list_targets_by_rule(Rule=rule_name)["Targets"]
    if targets:
        eb.remove_targets(Rule=rule_name, Ids=[t["Id"] for t in targets])
    eb.delete_rule(Name=rule_name)
    ok(rule_name)
except Exception as e:
    skip(rule_name, e)

# ── API Gateway v2 ────────────────────────────────────────────────────────────
print("API Gateway:")
apigw = session.client("apigatewayv2")
try:
    apis = apigw.get_apis()["Items"]
    for api in apis:
        if api["Name"] == f"{PREFIX}-docs-api":
            apigw.delete_api(ApiId=api["ApiId"])
            ok(f"{PREFIX}-docs-api ({api['ApiId']})")
except Exception as e:
    skip(f"{PREFIX}-docs-api", e)

# ── IAM Roles ─────────────────────────────────────────────────────────────────
print("IAM Roles:")
iam = session.client("iam")
for role in [f"{PREFIX}-pipeline-base", f"{PREFIX}-sfn", f"{PREFIX}-events-to-sfn"]:
    try:
        # Detach managed policies
        for p in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
        # Delete inline policies
        for name in iam.list_role_policies(RoleName=role)["PolicyNames"]:
            iam.delete_role_policy(RoleName=role, PolicyName=name)
        iam.delete_role(RoleName=role)
        ok(role)
    except iam.exceptions.NoSuchEntityException as e:
        skip(role, e)
    except Exception as e:
        skip(role, e)

# ── CloudWatch Log Groups ─────────────────────────────────────────────────────
print("CloudWatch Log Groups:")
cw = session.client("logs")
log_groups = [
    f"/aws/lambda/{PREFIX}-pipeline",
    f"/aws/lambda/{PREFIX}-api",
    f"/aws/lambda/{PREFIX}-rag",
    f"/aws/lambda/{PREFIX}-01-parse",
    f"/aws/lambda/{PREFIX}-02-classify",
    f"/aws/lambda/{PREFIX}-03-embed",
    f"/aws/lambda/{PREFIX}-04-graph",
    f"/aws/lambda/{PREFIX}-05-diff",
    f"/aws/lambda/{PREFIX}-06-timeline",
    f"/aws/lambda/{PREFIX}-07-persist",
    f"/aws/vendedlogs/states/{PREFIX}-pipeline",
]
for lg in log_groups:
    try:
        cw.delete_log_group(logGroupName=lg)
        ok(lg)
    except cw.exceptions.ResourceNotFoundException as e:
        skip(lg, e)
    except Exception as e:
        skip(lg, e)

# ── SQS ───────────────────────────────────────────────────────────────────────
print("SQS:")
sqs = session.client("sqs")
try:
    url = sqs.get_queue_url(QueueName=f"{PREFIX}-pipeline-dlq")["QueueUrl"]
    sqs.delete_queue(QueueUrl=url)
    ok(f"{PREFIX}-pipeline-dlq")
except sqs.exceptions.QueueDoesNotExist as e:
    skip(f"{PREFIX}-pipeline-dlq", e)
except Exception as e:
    skip(f"{PREFIX}-pipeline-dlq", e)

# ── DynamoDB ──────────────────────────────────────────────────────────────────
print("DynamoDB:")
ddb = session.client("dynamodb")
try:
    ddb.delete_table(TableName=f"{PREFIX}-main")
    ok(f"{PREFIX}-main")
except ddb.exceptions.ResourceNotFoundException as e:
    skip(f"{PREFIX}-main", e)
except Exception as e:
    skip(f"{PREFIX}-main", e)

# ── S3 Buckets ────────────────────────────────────────────────────────────────
print("S3 Buckets:")
s3 = session.client("s3")
s3r = session.resource("s3")
for bucket in [f"{PREFIX}-raw-{account}", f"{PREFIX}-processed-{account}"]:
    try:
        b = s3r.Bucket(bucket)
        b.object_versions.delete()
        b.objects.delete()
        s3.delete_bucket(Bucket=bucket)
        ok(bucket)
    except s3.exceptions.NoSuchBucket as e:
        skip(bucket, e)
    except Exception as e:
        skip(bucket, e)

# ── OpenSearch ────────────────────────────────────────────────────────────────
print("OpenSearch:")
es = session.client("opensearch")
try:
    es.delete_domain(DomainName=f"{PREFIX}-search")
    ok(f"{PREFIX}-search (deletion is async, takes ~10 min)")
except es.exceptions.ResourceNotFoundException as e:
    skip(f"{PREFIX}-search", e)
except Exception as e:
    skip(f"{PREFIX}-search", e)

print("\nCleanup complete. Ready for a fresh deploy.")
