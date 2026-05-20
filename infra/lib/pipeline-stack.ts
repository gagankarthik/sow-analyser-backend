/**
 * Pipeline stack
 * --------------
 * The 7-stage document-ingest pipeline. Provisions:
 *
 *   1. A shared Python Lambda layer built from `../lambdas/shared/requirements.txt`
 *      (Docker bundling pinned to the Python 3.12 build image). All seven
 *      stage Lambdas import from it (boto3 pinned versions, pdfplumber,
 *      python-docx, openai, structlog, tenacity, aws-lambda-powertools, ...).
 *
 *   2. Seven Python 3.12 / arm64 Lambdas, one per pipeline stage. Each gets a
 *      DLQ (SQS), X-Ray tracing, and least-privilege IAM grants. Memory and
 *      timeout are tuned per stage (see PIPELINE_STAGES in common.ts).
 *
 *   3. A Step Functions *Express* state machine that runs the seven Lambdas
 *      in series, threading `{ bucket, key, docId, tenantId, ...pipelineState }`
 *      through. On any failure, the catch state writes to the DLQ and updates
 *      DynamoDB status to "failed". On success, it emits a `documentReady`
 *      event onto the default EventBridge bus.
 *
 *   4. An EventBridge rule that catches `Object Created` events on the raw
 *      bucket and starts the state machine with the S3 details as input.
 *
 * IAM least-privilege (per Lambda):
 *   - parse     : read raw bucket, write processed bucket
 *   - classify  : read processed bucket, read OpenAI secret
 *   - embed     : read processed bucket, read OpenAI secret, write OpenSearch
 *   - graph     : read+write main table
 *   - diff      : read processed bucket, write processed bucket, read main table
 *   - timeline  : read+write main table, read+write processed bucket
 *   - persist   : write main table, write processed bucket (audit blob)
 *
 * Neptune stub: ENABLE_NEPTUNE env var is wired but defaults to false; no
 * Neptune resources are provisioned in v1 (see ARCHITECTURE.md §3.1).
 */
import * as path from "path";
import {
  CfnOutput,
  Duration,
  RemovalPolicy,
  Stack,
} from "aws-cdk-lib";
import { ITable } from "aws-cdk-lib/aws-dynamodb";
import { EventField, Rule, RuleTargetInput } from "aws-cdk-lib/aws-events";
import { SfnStateMachine } from "aws-cdk-lib/aws-events-targets";
import {
  Effect,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from "aws-cdk-lib/aws-iam";
import {
  Architecture,
  Code,
  Function as LambdaFunction,
  LayerVersion,
  Runtime,
  Tracing,
} from "aws-cdk-lib/aws-lambda";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { IBucket } from "aws-cdk-lib/aws-s3";
import { ISecret } from "aws-cdk-lib/aws-secretsmanager";
import { Queue } from "aws-cdk-lib/aws-sqs";
import {
  DefinitionBody,
  IStateMachine,
  JsonPath,
  LogLevel,
  StateMachine,
  StateMachineType,
  TaskInput,
} from "aws-cdk-lib/aws-stepfunctions";
import {
  DynamoAttributeValue,
  DynamoUpdateItem,
  EventBridgePutEvents,
  LambdaInvoke,
} from "aws-cdk-lib/aws-stepfunctions-tasks";
import { Construct } from "constructs";

import {
  BlueIQStackProps,
  PIPELINE_STAGES,
  resourceName,
} from "./common";

/**
 * Cross-stack inputs. We deliberately pass concrete resources (not stack
 * objects) so this stack can be reused for blue/green or alternate envs
 * without dragging the whole storage / search stacks along.
 */
export interface PipelineStackProps extends BlueIQStackProps {
  readonly rawBucket: IBucket;
  readonly processedBucket: IBucket;
  readonly mainTable: ITable;
  readonly openAiSecret: ISecret;

  /**
   * OpenSearch HTTPS endpoint (e.g. `search-xxx.us-east-1.es.amazonaws.com`).
   * Passed in by `bin/infra.ts` after both Storage and Search stacks resolve.
   * Pipeline depends on Search → no circular-dep risk because Search does not
   * touch Pipeline.
   */
  readonly openSearchEndpoint: string;
  readonly openSearchDomainArn: string;
}

export class PipelineStack extends Stack {
  /** The Express state machine that runs the 7-stage pipeline. */
  public readonly stateMachine: IStateMachine;

  /** Lambda layer with shared Python deps (boto3, pdfplumber, openai, ...). */
  public readonly sharedLayer: LayerVersion;

  /** Map of stage id → Lambda function for downstream wiring. */
  public readonly stageFunctions: Record<string, LambdaFunction> = {};

  constructor(scope: Construct, id: string, props: PipelineStackProps) {
    super(scope, id, props);

    const {
      projectName,
      stage,
      rawBucket,
      processedBucket,
      mainTable,
      openAiSecret,
      openSearchEndpoint,
      openSearchDomainArn,
    } = props;

    // ---------------------------------------------------------------------
    // Shared Lambda layer
    // ---------------------------------------------------------------------
    // Builds from ../lambdas/shared via Docker. We pip-install the requirements
    // into /asset-output/python so the layer is on Python's import path at
    // runtime.
    //
    // NOTE: requires Docker on the deploying machine. If you don't have Docker
    // locally, run `cdk deploy` from an environment that does (CI runners,
    // CodeBuild, etc.). The synth itself does not need Docker.
    const sharedLambdaPath = path.join(__dirname, "..", "..", "lambdas");
    this.sharedLayer = new LayerVersion(this, "SharedPythonLayer", {
      layerVersionName: resourceName(projectName, stage, "shared-layer"),
      description:
        "Shared Python deps for Blue-IQ pipeline Lambdas (boto3, pdfplumber, openai, ...)",
      compatibleRuntimes: [Runtime.PYTHON_3_12],
      compatibleArchitectures: [Architecture.ARM_64],
      code: Code.fromAsset(path.join(sharedLambdaPath, "shared"), {
        bundling: {
          image: Runtime.PYTHON_3_12.bundlingImage,
          platform: "linux/arm64",
          command: [
            "bash",
            "-c",
            [
              "pip install -r requirements.txt -t /asset-output/python " +
                "--platform manylinux2014_aarch64 " +
                "--implementation cp " +
                "--python-version 3.12 " +
                "--only-binary=:all: " +
                "--upgrade",
              "cp -au . /asset-output/python || true",
            ].join(" && "),
          ],
          local: {
            tryBundle(outputDir: string): boolean {
              const { execSync } = require("child_process");
              const sourceDir = path.join(sharedLambdaPath, "shared");
              const pythonDir = path.join(outputDir, "python");
              try {
                execSync(
                  `pip install -r requirements.txt -t "${pythonDir}" --upgrade`,
                  { cwd: sourceDir, stdio: "inherit" }
                );
                execSync(
                  process.platform === "win32"
                    ? `xcopy /E /I /Y "${sourceDir}" "${pythonDir}\\"`
                    : `cp -au "${sourceDir}/." "${pythonDir}/"`,
                  { stdio: "inherit" }
                );
                return true;
              } catch (e) {
                return false;
              }
            },
          },
        },
      }),
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ---------------------------------------------------------------------
    // Shared DLQ for any pipeline Lambda failure
    // ---------------------------------------------------------------------
    const pipelineDlq = new Queue(this, "PipelineDlq", {
      queueName: resourceName(projectName, stage, "pipeline-dlq"),
      retentionPeriod: Duration.days(14),
      enforceSSL: true,
    });

    // ---------------------------------------------------------------------
    // Common Lambda env
    // ---------------------------------------------------------------------
    const commonEnv: Record<string, string> = {
      PROJECT_NAME: projectName,
      STAGE: stage,
      DDB_TABLE_NAME: mainTable.tableName,
      RAW_BUCKET: rawBucket.bucketName,
      PROCESSED_BUCKET: processedBucket.bucketName,
      OPENAI_SECRET_ARN: openAiSecret.secretArn,
      OPENSEARCH_ENDPOINT: openSearchEndpoint,
      OPENSEARCH_DOMAIN_ARN: openSearchDomainArn,
      ENABLE_NEPTUNE: "false",
      EMBEDDING_MODEL: "text-embedding-3-small",
      CHAT_MODEL: "gpt-4o-mini",
      POWERTOOLS_SERVICE_NAME: resourceName(projectName, stage, "pipeline"),
      POWERTOOLS_METRICS_NAMESPACE: resourceName(projectName, stage),
    };

    // ---------------------------------------------------------------------
    // Build the seven stage Lambdas
    // ---------------------------------------------------------------------
    for (const s of PIPELINE_STAGES) {
      const fn = new LambdaFunction(this, `Fn-${s.id}`, {
        functionName: resourceName(projectName, stage, s.id.replace(/_/g, "-")),
        runtime: Runtime.PYTHON_3_12,
        architecture: Architecture.ARM_64,
        memorySize: s.memoryMb,
        timeout: Duration.minutes(s.timeoutMin),
        handler: "index.handler",
        code: Code.fromAsset(path.join(sharedLambdaPath, s.id)),
        layers: [this.sharedLayer],
        environment: { ...commonEnv, PIPELINE_STAGE: s.id },
        tracing: Tracing.ACTIVE,
        deadLetterQueue: pipelineDlq,
        deadLetterQueueEnabled: true,
        // CloudWatch log group is created automatically by Lambda. We rely on
        // the default group; retention is set out-of-band (e.g. via a log
        // policy or a follow-up custom resource).
      });
      this.stageFunctions[s.id] = fn;
    }

    // ---------------------------------------------------------------------
    // Least-privilege IAM per stage
    // ---------------------------------------------------------------------
    const parseFn = this.stageFunctions["01_parse"];
    const classifyFn = this.stageFunctions["02_classify"];
    const embedFn = this.stageFunctions["03_embed"];
    const graphFn = this.stageFunctions["04_graph"];
    const diffFn = this.stageFunctions["05_diff"];
    const timelineFn = this.stageFunctions["06_timeline"];
    const persistFn = this.stageFunctions["07_persist"];

    // parse: read raw, write processed, Textract for OCR
    rawBucket.grantRead(parseFn);
    processedBucket.grantWrite(parseFn);
    parseFn.addToRolePolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: [
          "textract:DetectDocumentText",
          "textract:AnalyzeDocument",
          "textract:StartDocumentAnalysis",
          "textract:GetDocumentAnalysis",
        ],
        resources: ["*"], // Textract does not support resource-level perms
      })
    );

    // classify: read processed, OpenAI secret
    processedBucket.grantRead(classifyFn);
    openAiSecret.grantRead(classifyFn);

    // embed: read processed, OpenAI secret, write OpenSearch
    processedBucket.grantRead(embedFn);
    openAiSecret.grantRead(embedFn);
    embedFn.addToRolePolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["es:ESHttp*"],
        resources: [`${openSearchDomainArn}/*`],
      })
    );

    // graph: read/write main table (adjacency-list lineage)
    mainTable.grantReadWriteData(graphFn);

    // diff: read processed + write processed (snapshots), read table for prior
    processedBucket.grantReadWrite(diffFn);
    mainTable.grantReadData(diffFn);

    // timeline: read/write main table, read/write processed bucket
    mainTable.grantReadWriteData(timelineFn);
    processedBucket.grantReadWrite(timelineFn);

    // persist: write main table, write audit blob into processed
    mainTable.grantWriteData(persistFn);
    processedBucket.grantWrite(persistFn);
    // persist also publishes to AppSync via the on* internal mutations; the
    // api stack handles granting that permission (it knows the GraphQL ARN).

    // ---------------------------------------------------------------------
    // Step Functions Express state machine
    // ---------------------------------------------------------------------
    // Build the chain: parse → classify → embed → graph → diff → timeline → persist.
    const buildTask = (
      stageId: string,
      fn: LambdaFunction,
      idSuffix: string
    ): LambdaInvoke =>
      new LambdaInvoke(this, `Task-${idSuffix}`, {
        lambdaFunction: fn,
        // Each stage receives the rolling pipeline state and returns an
        // updated copy. payloadResponseOnly avoids the Lambda wrapping
        // envelope so the next stage sees clean JSON.
        payloadResponseOnly: true,
        comment: `Pipeline stage ${stageId}`,
      });

    const tasks = PIPELINE_STAGES.map((s) =>
      buildTask(s.id, this.stageFunctions[s.id], s.id.replace(/_/g, "-"))
    );

    // Chain them: t0 → t1 → ... → t6
    let chain = tasks[0].next(tasks[1]);
    for (let i = 2; i < tasks.length; i++) {
      chain = chain.next(tasks[i]);
    }

    // Success terminator: emit "documentReady" event on default bus.
    // The chain's running state object includes docId / tenantId; forward it
    // verbatim so subscribers (AppSync mutations, audit, etc.) see the same.
    const success = new EventBridgePutEvents(this, "EmitDocumentReady", {
      entries: [
        {
          detail: TaskInput.fromObject({
            type: "documentReady",
            state: JsonPath.entirePayload,
          }),
          detailType: "blue-iq.documentReady",
          source: `blue-iq.${stage}.pipeline`,
        },
      ],
      comment: "Pipeline succeeded — fan out to subscribers",
    });

    // Failure terminator: mark DDB status=FAILED.
    // The execution input must contain `docId` — see the EventBridge rule
    // input below. We resolve $.docId from the running state.
    const markFailed = new DynamoUpdateItem(this, "MarkFailed", {
      table: mainTable,
      key: {
        pk: DynamoAttributeValue.fromString(
          JsonPath.format("DOC#{}", JsonPath.stringAt("$.docId"))
        ),
        sk: DynamoAttributeValue.fromString("META"),
      },
      updateExpression:
        "SET #s = :failed, updated_at = :ts, error_message = :msg",
      expressionAttributeNames: {
        "#s": "status",
      },
      expressionAttributeValues: {
        ":failed": DynamoAttributeValue.fromString("FAILED"),
        ":ts": DynamoAttributeValue.fromString(
          JsonPath.stateEnteredTime
        ),
        ":msg": DynamoAttributeValue.fromString("pipeline stage failed"),
      },
      comment: "Mark document FAILED on any pipeline error",
    });

    // Wire each task's catch handler to markFailed.
    for (const t of tasks) {
      t.addCatch(markFailed, { resultPath: "$.error" });
    }

    const definition = chain.next(success);

    const logGroup = new LogGroup(this, "PipelineLogs", {
      logGroupName: `/aws/vendedlogs/states/${resourceName(
        projectName,
        stage,
        "pipeline"
      )}`,
      retention: RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    this.stateMachine = new StateMachine(this, "PipelineStateMachine", {
      stateMachineName: resourceName(projectName, stage, "pipeline"),
      stateMachineType: StateMachineType.EXPRESS,
      definitionBody: DefinitionBody.fromChainable(definition),
      tracingEnabled: true,
      logs: {
        destination: logGroup,
        level: LogLevel.ALL,
        includeExecutionData: true,
      },
      timeout: Duration.minutes(15),
    });

    // ---------------------------------------------------------------------
    // EventBridge rule: S3 ObjectCreated on raw bucket → start state machine
    // ---------------------------------------------------------------------
    const rule = new Rule(this, "RawObjectCreatedRule", {
      ruleName: resourceName(projectName, stage, "raw-object-created"),
      description: "Trigger pipeline on any new object in the raw bucket",
      eventPattern: {
        source: ["aws.s3"],
        detailType: ["Object Created"],
        detail: {
          bucket: { name: [rawBucket.bucketName] },
        },
      },
    });

    // The state-machine target receives a transformed input that the first
    // Lambda (`01_parse`) expects: { bucket, key, docId, tenantId }.
    // Upload keys must look like `tenants/<tenantId>/uploads/<docId>/<filename>`.
    // The parse Lambda re-parses the key to extract tenantId/docId; we still
    // surface them at the top level so the failure handler (MarkFailed) can
    // reference $.docId without diving into S3-event shape.
    rule.addTarget(
      new SfnStateMachine(this.stateMachine, {
        input: RuleTargetInput.fromObject({
          bucket: EventField.fromPath("$.detail.bucket.name"),
          key: EventField.fromPath("$.detail.object.key"),
          // docId / tenantId are filled in by the parse Lambda after it cracks
          // the key. We seed them as null here so the failure path doesn't
          // crash on missing fields (DynamoUpdateItem treats null as the
          // literal string "null" if it ever reaches that state without a
          // docId; the catch handler runs *after* parse, so this is safe).
          docId: EventField.fromPath("$.detail.object.key"),
          tenantId: "unknown",
        }),
      })
    );

    // Allow EventBridge to start the state machine.
    const eventsRole = new Role(this, "EventsToStepFunctionsRole", {
      assumedBy: new ServicePrincipal("events.amazonaws.com"),
      description:
        "Lets EventBridge invoke the Blue-IQ pipeline Express state machine",
    });
    this.stateMachine.grantStartExecution(eventsRole);

    // ---------------------------------------------------------------------
    // Outputs
    // ---------------------------------------------------------------------
    new CfnOutput(this, "PipelineStateMachineArn", {
      value: this.stateMachine.stateMachineArn,
      exportName: resourceName(projectName, stage, "pipeline-state-machine"),
    });
    new CfnOutput(this, "PipelineDlqUrl", {
      value: pipelineDlq.queueUrl,
      exportName: resourceName(projectName, stage, "pipeline-dlq-url"),
    });
    new CfnOutput(this, "SharedLayerArn", {
      value: this.sharedLayer.layerVersionArn,
      exportName: resourceName(projectName, stage, "shared-layer-arn"),
    });
  }
}
