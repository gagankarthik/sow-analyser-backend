#!/usr/bin/env node
/**
 * Blue-IQ CDK app entrypoint.
 *
 * Composes four stacks in order:
 *
 *   Storage ──┐
 *             ├──► Pipeline (depends on Storage + Search)
 *   Search ───┘
 *
 *   Api  ◄──── Storage (DDB table + processed bucket + OpenAI secret)
 *        ◄──── Search  (OpenSearch endpoint, for the RAG resolver Lambda)
 *
 * Stack naming convention: `${ProjectName}-${Stage}-${Name}` in PascalCase,
 * e.g. `BlueIq-Dev-Storage`. Physical resources use lowercase kebab,
 * e.g. `blue-iq-dev-main`.
 *
 * Context (set in `cdk.json`):
 *   - projectName  (string, default "blue-iq")
 *   - stage        (string, default "dev")
 *
 * Both can be overridden at the CLI:
 *   cdk deploy -c stage=prod -c projectName=blue-iq
 */
import "source-map-support/register";
import { App, Tags } from "aws-cdk-lib";

import { commonTags, logicalId } from "../lib/common";
import { ApiStack } from "../lib/api-stack";
import { PipelineStack } from "../lib/pipeline-stack";
import { SearchStack } from "../lib/search-stack";
import { StorageStack } from "../lib/storage-stack";

const app = new App();

// ---------------------------------------------------------------------------
// Context resolution
// ---------------------------------------------------------------------------
const projectName = (app.node.tryGetContext("projectName") ?? "blue-iq") as string;
const stage = (app.node.tryGetContext("stage") ?? "dev") as string;

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT ?? process.env.AWS_ACCOUNT_ID,
  region: process.env.CDK_DEFAULT_REGION ?? process.env.AWS_REGION ?? "us-east-1",
};

// ---------------------------------------------------------------------------
// Stack instantiation
// ---------------------------------------------------------------------------
const storage = new StorageStack(
  app,
  logicalId(projectName, stage, "Storage"),
  { projectName, stage, env, description: "Blue-IQ persistent storage" }
);

const search = new SearchStack(
  app,
  logicalId(projectName, stage, "Search"),
  { projectName, stage, env, description: "Blue-IQ OpenSearch domain" }
);

const pipeline = new PipelineStack(
  app,
  logicalId(projectName, stage, "Pipeline"),
  {
    projectName,
    stage,
    env,
    description: "Blue-IQ ingest pipeline (Lambdas + Step Functions)",
    rawBucket: storage.rawBucket,
    processedBucket: storage.processedBucket,
    mainTable: storage.mainTable,
    openAiSecret: storage.openAiSecret,
    openSearchEndpoint: search.domain.domainEndpoint,
    openSearchDomainArn: search.domain.domainArn,
  }
);

const api = new ApiStack(app, logicalId(projectName, stage, "Api"), {
  projectName,
  stage,
  env,
  description: "Blue-IQ AppSync GraphQL API",
  mainTable: storage.mainTable,
  processedBucket: storage.processedBucket,
  openAiSecret: storage.openAiSecret,
  openSearchEndpoint: search.domain.domainEndpoint,
  openSearchDomainArn: search.domain.domainArn,
});

// Explicit dependency wiring so CDK / CFN deploy order is correct.
pipeline.addDependency(storage);
pipeline.addDependency(search);
api.addDependency(storage);
api.addDependency(search);

// ---------------------------------------------------------------------------
// Universal tagging
// ---------------------------------------------------------------------------
const tags = commonTags(projectName, stage);
for (const stack of [storage, search, pipeline, api]) {
  for (const [k, v] of Object.entries(tags)) {
    Tags.of(stack).add(k, v);
  }
}

app.synth();
