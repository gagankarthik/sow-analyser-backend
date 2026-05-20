/**
 * API stack
 * ---------
 * Provisions the AppSync GraphQL API that the Next.js frontend (and any
 * service-to-service caller via SigV4) consumes.
 *
 * - Schema lives in ../api/schema.graphql.
 * - Primary auth: IAM (server-to-server from Next.js server actions /
 *   internal Lambdas that emit subscription mutations).
 * - Additional auth: API_KEY for local development. Rotate periodically.
 * - Cognito user pools are an intentional non-goal in v1 — add as an
 *   additional auth mode when the frontend gains user accounts.
 *
 * Data sources:
 *   - DynamoDB main table (for getDocument, listVersions, listChanges, etc.)
 *   - Lambda RAG resolver for askBluely (streaming via subscription on
 *     bluelyTokens — the resolver Lambda publishes onBluelyToken mutations
 *     against this same API using IAM auth).
 *
 * Resolvers:
 *   - VTL for trivial DDB reads (getDocument, listVersions, listChanges).
 *   - Lambda for askBluely (orchestrates OpenSearch hybrid search + OpenAI).
 *   - searchClauses also routes to the same RAG Lambda for now (it does the
 *     OpenSearch hit).
 *
 * Subscriptions:
 *   - documentStatusChanged / versionAdded / bluelyTokens are wired in the
 *     schema via @aws_subscribe; AppSync handles fan-out automatically when
 *     the corresponding mutations resolve.
 */
import * as path from "path";
import {
  CfnOutput,
  Duration,
  Expiration,
  Stack,
} from "aws-cdk-lib";
import {
  AuthorizationType,
  Definition,
  FieldLogLevel,
  GraphqlApi,
  MappingTemplate,
  SchemaFile,
} from "aws-cdk-lib/aws-appsync";
import { ITable } from "aws-cdk-lib/aws-dynamodb";
import { IBucket } from "aws-cdk-lib/aws-s3";
import { ISecret } from "aws-cdk-lib/aws-secretsmanager";
import {
  Architecture,
  Code,
  Function as LambdaFunction,
  Runtime,
  Tracing,
} from "aws-cdk-lib/aws-lambda";
import { Effect, PolicyStatement } from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

import { BlueIQStackProps, resourceName } from "./common";

export interface ApiStackProps extends BlueIQStackProps {
  readonly mainTable: ITable;
  readonly processedBucket: IBucket;
  readonly openAiSecret: ISecret;
  readonly openSearchEndpoint: string;
  readonly openSearchDomainArn: string;
}

export class ApiStack extends Stack {
  public readonly api: GraphqlApi;
  public readonly ragResolver: LambdaFunction;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    const {
      projectName,
      stage,
      mainTable,
      processedBucket,
      openAiSecret,
      openSearchEndpoint,
      openSearchDomainArn,
    } = props;

    // ---------------------------------------------------------------------
    // GraphQL API
    // ---------------------------------------------------------------------
    const schemaPath = path.join(__dirname, "..", "..", "api", "schema.graphql");

    this.api = new GraphqlApi(this, "GraphqlApi", {
      name: resourceName(projectName, stage, "api"),
      definition: Definition.fromSchema(SchemaFile.fromAsset(schemaPath)),
      authorizationConfig: {
        // IAM is primary so Next.js server actions can SigV4-sign requests
        // and pipeline Lambdas can publish subscription mutations.
        defaultAuthorization: {
          authorizationType: AuthorizationType.IAM,
        },
        additionalAuthorizationModes: [
          {
            authorizationType: AuthorizationType.API_KEY,
            apiKeyConfig: {
              name: resourceName(projectName, stage, "dev-key"),
              description: "Dev-only API key — rotate or remove for prod",
              expires: Expiration.after(Duration.days(365)),
            },
          },
        ],
      },
      logConfig: {
        fieldLogLevel: FieldLogLevel.ERROR,
        excludeVerboseContent: true,
      },
      xrayEnabled: true,
    });

    // ---------------------------------------------------------------------
    // RAG resolver Lambda (askBluely + searchClauses)
    // ---------------------------------------------------------------------
    // Lives outside the pipeline Lambdas (`08_rag`) to keep auth concerns
    // separate. We do NOT add it to the layered shared deps here — the RAG
    // resolver has its own slim deps (openai + opensearch-py). It can be
    // promoted to use the shared layer later by passing it via cross-stack.
    //
    // The asset path is `../lambdas/08_rag`. The lambdas/ owner will create
    // this folder; we reference it eagerly so wiring is in place.
    const ragLambdaPath = path.join(
      __dirname,
      "..",
      "..",
      "lambdas",
      "08_rag"
    );

    this.ragResolver = new LambdaFunction(this, "RagResolver", {
      functionName: resourceName(projectName, stage, "rag-resolver"),
      runtime: Runtime.PYTHON_3_12,
      architecture: Architecture.ARM_64,
      memorySize: 1024,
      timeout: Duration.minutes(2),
      handler: "index.handler",
      code: Code.fromAsset(ragLambdaPath),
      tracing: Tracing.ACTIVE,
      environment: {
        PROJECT_NAME: projectName,
        STAGE: stage,
        DDB_TABLE_NAME: mainTable.tableName,
        PROCESSED_BUCKET: processedBucket.bucketName,
        OPENAI_SECRET_ARN: openAiSecret.secretArn,
        OPENSEARCH_ENDPOINT: openSearchEndpoint,
        APPSYNC_API_ID: this.api.apiId,
        APPSYNC_GRAPHQL_ENDPOINT: this.api.graphqlUrl,
        EMBEDDING_MODEL: "text-embedding-3-small",
        CHAT_MODEL: "gpt-4o-mini",
      },
    });

    // RAG resolver needs:
    // - DDB read (cite the source clause back into a document context)
    // - processed bucket read (load full clause text on cite)
    // - OpenAI secret read
    // - OpenSearch HTTP (for hybrid search)
    // - AppSync mutation rights (to publish onBluelyToken streaming events)
    mainTable.grantReadData(this.ragResolver);
    processedBucket.grantRead(this.ragResolver);
    openAiSecret.grantRead(this.ragResolver);
    this.ragResolver.addToRolePolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["es:ESHttp*"],
        resources: [`${openSearchDomainArn}/*`],
      })
    );
    this.ragResolver.addToRolePolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["appsync:GraphQL"],
        resources: [`${this.api.arn}/types/Mutation/fields/onBluelyToken`],
      })
    );

    // Also grant the *pipeline* persist Lambda permission to call the
    // onDocumentStatusChanged / onVersionAdded mutations. We don't have a
    // direct reference here (cross-stack), so we publish a wildcard policy
    // attached to the API and let the pipeline-stack grant Lambdas by ARN
    // when needed. For now: nothing — the persist Lambda can sign IAM with
    // the API ARN if its role has appsync:GraphQL on this api.

    // ---------------------------------------------------------------------
    // Data sources + resolvers
    // ---------------------------------------------------------------------
    const ddbDs = this.api.addDynamoDbDataSource(
      "MainTableDs",
      mainTable,
      {
        name: "MainTableDataSource",
        description: "Blue-IQ single-table DDB",
      }
    );

    const ragDs = this.api.addLambdaDataSource("RagDs", this.ragResolver, {
      name: "RagLambdaDataSource",
      description: "Hybrid OpenSearch + OpenAI resolver",
    });

    // ---- VTL resolvers (simple DDB reads) -------------------------------
    // getDocument(id) → PK=DOC#<id>, SK=META
    ddbDs.createResolver("GetDocumentResolver", {
      typeName: "Query",
      fieldName: "getDocument",
      requestMappingTemplate: MappingTemplate.fromString(`
{
  "version": "2018-05-29",
  "operation": "GetItem",
  "key": {
    "pk": $util.dynamodb.toDynamoDBJson("DOC#$ctx.args.id"),
    "sk": $util.dynamodb.toDynamoDBJson("META")
  }
}
`),
      responseMappingTemplate: MappingTemplate.fromString(
        `$util.toJson($ctx.result)`
      ),
    });

    // listVersions(documentId) → PK=DOC#<id>, SK begins_with V#
    ddbDs.createResolver("ListVersionsResolver", {
      typeName: "Query",
      fieldName: "listVersions",
      requestMappingTemplate: MappingTemplate.fromString(`
{
  "version": "2018-05-29",
  "operation": "Query",
  "query": {
    "expression": "#pk = :pk AND begins_with(#sk, :prefix)",
    "expressionNames": { "#pk": "pk", "#sk": "sk" },
    "expressionValues": {
      ":pk": $util.dynamodb.toDynamoDBJson("DOC#$ctx.args.documentId"),
      ":prefix": $util.dynamodb.toDynamoDBJson("V#")
    }
  },
  "scanIndexForward": true
}
`),
      responseMappingTemplate: MappingTemplate.fromString(
        `$util.toJson($ctx.result.items)`
      ),
    });

    // listChanges(documentId, fromVersion?, toVersion?) → PK=DOC#<id>, SK begins_with CHG#
    ddbDs.createResolver("ListChangesResolver", {
      typeName: "Query",
      fieldName: "listChanges",
      requestMappingTemplate: MappingTemplate.fromString(`
{
  "version": "2018-05-29",
  "operation": "Query",
  "query": {
    "expression": "#pk = :pk AND begins_with(#sk, :prefix)",
    "expressionNames": { "#pk": "pk", "#sk": "sk" },
    "expressionValues": {
      ":pk": $util.dynamodb.toDynamoDBJson("DOC#$ctx.args.documentId"),
      ":prefix": $util.dynamodb.toDynamoDBJson("CHG#")
    }
  }
}
`),
      responseMappingTemplate: MappingTemplate.fromString(`
#set($items = $ctx.result.items)
#if($ctx.args.fromVersion)
  #set($filtered = [])
  #foreach($i in $items)
    #if($i.fromVersion >= $ctx.args.fromVersion) $util.qr($filtered.add($i)) #end
  #end
  #set($items = $filtered)
#end
#if($ctx.args.toVersion)
  #set($filtered = [])
  #foreach($i in $items)
    #if($i.toVersion <= $ctx.args.toVersion) $util.qr($filtered.add($i)) #end
  #end
  #set($items = $filtered)
#end
$util.toJson($items)
`),
    });

    // listDocumentsByTenant → GSI1 query
    ddbDs.createResolver("ListDocumentsByTenantResolver", {
      typeName: "Query",
      fieldName: "listDocumentsByTenant",
      requestMappingTemplate: MappingTemplate.fromString(`
{
  "version": "2018-05-29",
  "operation": "Query",
  "index": "gsi1-tenant-created",
  "query": {
    "expression": "#tid = :tid",
    "expressionNames": { "#tid": "tenant_id" },
    "expressionValues": {
      ":tid": $util.dynamodb.toDynamoDBJson($ctx.args.tenantId)
    }
  },
  "limit": $util.defaultIfNull($ctx.args.limit, 20),
  #if($ctx.args.nextToken)
  "nextToken": "$ctx.args.nextToken",
  #end
  "scanIndexForward": false
}
`),
      responseMappingTemplate: MappingTemplate.fromString(`
{
  "items": $util.toJson($ctx.result.items),
  "nextToken": #if($ctx.result.nextToken) "$ctx.result.nextToken" #else null #end
}
`),
    });

    // ---- Lambda resolvers (RAG + search) --------------------------------
    ragDs.createResolver("SearchClausesResolver", {
      typeName: "Query",
      fieldName: "searchClauses",
    });

    ragDs.createResolver("AskBluelyResolver", {
      typeName: "Mutation",
      fieldName: "askBluely",
    });

    // None-data-source resolvers for the internal mutation fan-out
    // (onDocumentStatusChanged, onVersionAdded, onBluelyToken). These just
    // echo the input back so AppSync triggers @aws_subscribe.
    const noneDs = this.api.addNoneDataSource("NoneDs", {
      name: "PassThroughNoneDs",
      description: "Echo input for subscription fan-out",
    });

    const passThroughReq = (argsKey: string): string => `
{
  "version": "2018-05-29",
  "payload": $util.toJson($ctx.args.${argsKey})
}
`;
    const passThroughRes = MappingTemplate.fromString(
      `$util.toJson($ctx.result)`
    );

    noneDs.createResolver("OnDocumentStatusChangedResolver", {
      typeName: "Mutation",
      fieldName: "onDocumentStatusChanged",
      requestMappingTemplate: MappingTemplate.fromString(passThroughReq("input")),
      responseMappingTemplate: passThroughRes,
    });
    noneDs.createResolver("OnVersionAddedResolver", {
      typeName: "Mutation",
      fieldName: "onVersionAdded",
      requestMappingTemplate: MappingTemplate.fromString(passThroughReq("input")),
      responseMappingTemplate: passThroughRes,
    });
    noneDs.createResolver("OnBluelyTokenResolver", {
      typeName: "Mutation",
      fieldName: "onBluelyToken",
      requestMappingTemplate: MappingTemplate.fromString(passThroughReq("input")),
      responseMappingTemplate: passThroughRes,
    });

    // ---------------------------------------------------------------------
    // Outputs
    // ---------------------------------------------------------------------
    new CfnOutput(this, "GraphqlApiId", {
      value: this.api.apiId,
      exportName: resourceName(projectName, stage, "graphql-api-id"),
    });
    new CfnOutput(this, "GraphqlApiUrl", {
      value: this.api.graphqlUrl,
      exportName: resourceName(projectName, stage, "graphql-api-url"),
    });
    new CfnOutput(this, "GraphqlApiArn", {
      value: this.api.arn,
      exportName: resourceName(projectName, stage, "graphql-api-arn"),
    });
    if (this.api.apiKey) {
      new CfnOutput(this, "GraphqlApiKey", {
        value: this.api.apiKey,
        description:
          "Dev API key (sensitive). Use only for local development.",
        exportName: resourceName(projectName, stage, "graphql-api-key"),
      });
    }
  }
}
