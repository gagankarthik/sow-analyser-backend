/**
 * Shared types, naming helpers, and common props used across all Blue-IQ stacks.
 *
 * Naming convention: every physical AWS resource is named
 *   `${projectName}-${stage}-<resource>`
 * and every logical CFN stack is named
 *   `${projectName}-${stage}-<stackName>` (PascalCase preserved by CDK).
 */
import { StackProps } from "aws-cdk-lib";

/**
 * Props that every stack in the Blue-IQ system accepts.
 * `projectName` and `stage` come from cdk context (cdk.json).
 */
export interface BlueIQStackProps extends StackProps {
  readonly projectName: string;
  readonly stage: string;
}

/**
 * Build a hyphen-joined resource name like `blue-iq-dev-main`.
 * Lower-cases segments to play nicely with S3 / DDB / OpenSearch.
 */
export function resourceName(
  projectName: string,
  stage: string,
  ...parts: string[]
): string {
  return [projectName, stage, ...parts]
    .filter((p) => p && p.length > 0)
    .map((p) => p.toLowerCase())
    .join("-");
}

/**
 * Build a PascalCase logical id like `BlueIqDevMain` for CFN logical names.
 */
export function logicalId(
  projectName: string,
  stage: string,
  ...parts: string[]
): string {
  return [projectName, stage, ...parts]
    .filter((p) => p && p.length > 0)
    .map((p) =>
      p
        .split(/[-_\s]+/)
        .map((s) => (s.length === 0 ? s : s[0].toUpperCase() + s.slice(1)))
        .join("")
    )
    .join("");
}

/**
 * Common tags applied to every stack via `Tags.of(stack).add(...)`.
 */
export function commonTags(
  projectName: string,
  stage: string
): Record<string, string> {
  return {
    Project: projectName,
    Stage: stage,
    ManagedBy: "CDK",
  };
}

/**
 * Pipeline stage identifiers (1-7). Used to name lambdas + step function tasks.
 */
export const PIPELINE_STAGES = [
  { id: "01_parse", display: "Parse", memoryMb: 2048, timeoutMin: 15 },
  { id: "02_classify", display: "Classify", memoryMb: 1024, timeoutMin: 5 },
  { id: "03_embed", display: "Embed", memoryMb: 2048, timeoutMin: 5 },
  { id: "04_graph", display: "Graph", memoryMb: 1024, timeoutMin: 5 },
  { id: "05_diff", display: "Diff", memoryMb: 1024, timeoutMin: 5 },
  { id: "06_timeline", display: "Timeline", memoryMb: 1024, timeoutMin: 5 },
  { id: "07_persist", display: "Persist", memoryMb: 1024, timeoutMin: 5 },
] as const;

export type PipelineStage = (typeof PIPELINE_STAGES)[number];

/**
 * DynamoDB single-table key conventions.
 * Documented here so every Lambda can import the same constants if needed
 * (they can be re-declared in Python, but having one source of truth helps).
 */
export const DDB_KEYS = {
  partitionKey: "pk",
  sortKey: "sk",
  ttlAttribute: "expires_at",
  gsi1: {
    name: "gsi1-tenant-created",
    partitionKey: "tenant_id",
    sortKey: "created_at",
  },
  gsi2: {
    name: "gsi2-status-updated",
    partitionKey: "status",
    sortKey: "updated_at",
  },
} as const;

/**
 * Environment toggle defaults. Kept here so they survive cross-stack imports.
 */
export const FEATURE_FLAGS = {
  enableNeptune: false,
  enableOpenSearch: true,
  enableAppSync: true,
} as const;
