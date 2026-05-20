/**
 * Storage stack
 * -------------
 * Provisions the durable state for Blue-IQ:
 *
 *   1. Two S3 buckets:
 *        - `${project}-${stage}-raw`        — presigned PUT target from the
 *          Next.js app. EventBridge-enabled so ObjectCreated emits to the
 *          default bus, which downstream triggers the Step Functions pipeline.
 *        - `${project}-${stage}-processed`  — extracted JSON, diff snapshots,
 *          audit blobs written by pipeline Lambdas.
 *
 *      Both buckets: SSE-S3, versioned, blocked public access, retain on
 *      destroy. Raw bucket has a lifecycle rule (Standard → IA @ 30d → Glacier
 *      @ 90d) and a CORS rule for the Next.js origin.
 *
 *   2. DynamoDB single-table `${project}-${stage}-main` with composite keys
 *      (pk/sk), on-demand billing, PITR, TTL on `expires_at`, and Streams
 *      (NEW_AND_OLD_IMAGES). Two GSIs:
 *        - gsi1: tenant_id / created_at  (tenant-scoped listing)
 *        - gsi2: status / updated_at     ("what's processing now")
 *
 *   3. Secrets Manager secret `${project}-${stage}-openai` (empty initial
 *      value — operator fills via console). Used by the classify and embed
 *      Lambdas at runtime.
 *
 * All ARNs / names are exported as CfnOutputs so the pipeline and api stacks
 * can consume them as constructor props.
 */
import { CfnOutput, Duration, RemovalPolicy, Stack } from "aws-cdk-lib";
import {
  AttributeType,
  BillingMode,
  StreamViewType,
  Table,
  TableEncryption,
} from "aws-cdk-lib/aws-dynamodb";
import {
  Bucket,
  BucketEncryption,
  CorsRule,
  HttpMethods,
  LifecycleRule,
  StorageClass,
} from "aws-cdk-lib/aws-s3";
import { Secret } from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

import { BlueIQStackProps, DDB_KEYS, resourceName } from "./common";

export class StorageStack extends Stack {
  /** Bucket receiving presigned uploads from the Next.js app. */
  public readonly rawBucket: Bucket;

  /** Bucket holding pipeline outputs (extracted JSON, diffs, audit blobs). */
  public readonly processedBucket: Bucket;

  /** Single-table DynamoDB. */
  public readonly mainTable: Table;

  /** Secret holding the OpenAI API key (filled out of band). */
  public readonly openAiSecret: Secret;

  constructor(scope: Construct, id: string, props: BlueIQStackProps) {
    super(scope, id, props);

    const { projectName, stage } = props;

    // ---------------------------------------------------------------------
    // S3 — raw uploads bucket
    // ---------------------------------------------------------------------
    const nextjsOrigin = process.env.NEXTJS_ORIGIN ?? "http://localhost:3000";

    const rawCors: CorsRule = {
      allowedOrigins: [nextjsOrigin],
      allowedMethods: [
        HttpMethods.GET,
        HttpMethods.PUT,
        HttpMethods.POST,
        HttpMethods.HEAD,
      ],
      allowedHeaders: ["*"],
      exposedHeaders: ["ETag", "x-amz-version-id"],
      maxAge: 3000,
    };

    const rawLifecycle: LifecycleRule = {
      id: "raw-cold-storage",
      enabled: true,
      transitions: [
        {
          storageClass: StorageClass.INFREQUENT_ACCESS,
          transitionAfter: Duration.days(30),
        },
        {
          storageClass: StorageClass.GLACIER,
          transitionAfter: Duration.days(90),
        },
      ],
      noncurrentVersionExpiration: Duration.days(365),
    };

    this.rawBucket = new Bucket(this, "RawBucket", {
      bucketName: resourceName(projectName, stage, "raw"),
      encryption: BucketEncryption.S3_MANAGED,
      versioned: true,
      blockPublicAccess: {
        blockPublicAcls: true,
        blockPublicPolicy: true,
        ignorePublicAcls: true,
        restrictPublicBuckets: true,
      },
      eventBridgeEnabled: true, // ObjectCreated emits to default event bus
      cors: [rawCors],
      lifecycleRules: [rawLifecycle],
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // ---------------------------------------------------------------------
    // S3 — processed artifacts bucket
    // ---------------------------------------------------------------------
    this.processedBucket = new Bucket(this, "ProcessedBucket", {
      bucketName: resourceName(projectName, stage, "processed"),
      encryption: BucketEncryption.S3_MANAGED,
      versioned: true,
      blockPublicAccess: {
        blockPublicAcls: true,
        blockPublicPolicy: true,
        ignorePublicAcls: true,
        restrictPublicBuckets: true,
      },
      enforceSSL: true,
      lifecycleRules: [
        {
          id: "processed-cold-storage",
          enabled: true,
          transitions: [
            {
              storageClass: StorageClass.INFREQUENT_ACCESS,
              transitionAfter: Duration.days(60),
            },
          ],
          noncurrentVersionExpiration: Duration.days(365),
        },
      ],
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // ---------------------------------------------------------------------
    // DynamoDB — single table
    // ---------------------------------------------------------------------
    this.mainTable = new Table(this, "MainTable", {
      tableName: resourceName(projectName, stage, "main"),
      partitionKey: {
        name: DDB_KEYS.partitionKey,
        type: AttributeType.STRING,
      },
      sortKey: { name: DDB_KEYS.sortKey, type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: DDB_KEYS.ttlAttribute,
      stream: StreamViewType.NEW_AND_OLD_IMAGES,
      encryption: TableEncryption.AWS_MANAGED,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // GSI1 — tenant-scoped listing
    this.mainTable.addGlobalSecondaryIndex({
      indexName: DDB_KEYS.gsi1.name,
      partitionKey: {
        name: DDB_KEYS.gsi1.partitionKey,
        type: AttributeType.STRING,
      },
      sortKey: {
        name: DDB_KEYS.gsi1.sortKey,
        type: AttributeType.STRING,
      },
    });

    // GSI2 — what's processing now
    this.mainTable.addGlobalSecondaryIndex({
      indexName: DDB_KEYS.gsi2.name,
      partitionKey: {
        name: DDB_KEYS.gsi2.partitionKey,
        type: AttributeType.STRING,
      },
      sortKey: {
        name: DDB_KEYS.gsi2.sortKey,
        type: AttributeType.STRING,
      },
    });

    // ---------------------------------------------------------------------
    // Secrets Manager — OpenAI API key
    // ---------------------------------------------------------------------
    // Operator fills `apiKey` field via the console after first deploy.
    // We seed with a placeholder JSON so the secret shape is `{ apiKey: "..." }`.
    this.openAiSecret = new Secret(this, "OpenAiSecret", {
      secretName: resourceName(projectName, stage, "openai"),
      description: "OpenAI API key used by classify + embed + RAG Lambdas",
      generateSecretString: {
        secretStringTemplate: JSON.stringify({ apiKey: "" }),
        generateStringKey: "placeholder",
        excludePunctuation: true,
        passwordLength: 16,
      },
    });

    // ---------------------------------------------------------------------
    // Outputs (cross-stack)
    // ---------------------------------------------------------------------
    new CfnOutput(this, "RawBucketName", {
      value: this.rawBucket.bucketName,
      exportName: resourceName(projectName, stage, "raw-bucket-name"),
    });
    new CfnOutput(this, "RawBucketArn", {
      value: this.rawBucket.bucketArn,
      exportName: resourceName(projectName, stage, "raw-bucket-arn"),
    });
    new CfnOutput(this, "ProcessedBucketName", {
      value: this.processedBucket.bucketName,
      exportName: resourceName(projectName, stage, "processed-bucket-name"),
    });
    new CfnOutput(this, "ProcessedBucketArn", {
      value: this.processedBucket.bucketArn,
      exportName: resourceName(projectName, stage, "processed-bucket-arn"),
    });
    new CfnOutput(this, "MainTableName", {
      value: this.mainTable.tableName,
      exportName: resourceName(projectName, stage, "main-table-name"),
    });
    new CfnOutput(this, "MainTableArn", {
      value: this.mainTable.tableArn,
      exportName: resourceName(projectName, stage, "main-table-arn"),
    });
    new CfnOutput(this, "MainTableStreamArn", {
      value: this.mainTable.tableStreamArn ?? "no-stream",
      exportName: resourceName(projectName, stage, "main-table-stream-arn"),
    });
    new CfnOutput(this, "OpenAiSecretArn", {
      value: this.openAiSecret.secretArn,
      exportName: resourceName(projectName, stage, "openai-secret-arn"),
    });
  }
}

