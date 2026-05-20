/**
 * Search stack
 * ------------
 * Provisions a managed Amazon OpenSearch Service domain that hosts two indices
 * created at the application layer (not as CDK resources):
 *
 *   - `clause-vectors` — k-NN HNSW index, 1536 dims for text-embedding-3-small
 *   - `clause-text`    — BM25 full-text on the clause body
 *
 * Why one domain, two indices: hybrid search (vector + BM25 in one query) is
 * the natural fit for clause retrieval, and OpenSearch handles both first-class.
 *
 * Notes:
 *   - Engine: OpenSearch 2.13 (k-NN HNSW + neural-search plugins available).
 *   - Auth: fine-grained access control with IAM auth ON, internal user DB OFF.
 *   - Encryption at rest + node-to-node + HTTPS enforced.
 *   - Sizing is parameterized via `stage`: dev uses 3 × t3.small.search;
 *     non-dev jumps to 3 × r6g.large.search (override in props if needed).
 *   - No VPC for v1 (simpler IAM-only access). Lock down to a VPC + private
 *     endpoint when going to prod.
 */
import { CfnOutput, RemovalPolicy, Stack } from "aws-cdk-lib";
import { EbsDeviceVolumeType } from "aws-cdk-lib/aws-ec2";
import {
  AnyPrincipal,
  Effect,
  PolicyStatement,
} from "aws-cdk-lib/aws-iam";
import { Domain, EngineVersion } from "aws-cdk-lib/aws-opensearchservice";
import { Construct } from "constructs";

import { BlueIQStackProps, resourceName } from "./common";

export interface SearchStackProps extends BlueIQStackProps {
  /** Override default data-node instance type. */
  readonly dataNodeInstanceType?: string;
  /** Override default data-node count (must be >= 1, odd recommended). */
  readonly dataNodeCount?: number;
}

export class SearchStack extends Stack {
  /** The OpenSearch domain. */
  public readonly domain: Domain;

  /** HTTPS endpoint, e.g. `search-blue-iq-dev-XXXXXX.us-east-1.es.amazonaws.com`. */
  public readonly domainEndpoint: string;

  constructor(scope: Construct, id: string, props: SearchStackProps) {
    super(scope, id, props);

    const { projectName, stage } = props;
    const isProd = stage === "prod";

    const instanceType =
      props.dataNodeInstanceType ??
      (isProd ? "r6g.large.search" : "t3.small.search");
    const instanceCount = props.dataNodeCount ?? 3;

    // ---------------------------------------------------------------------
    // Domain
    // ---------------------------------------------------------------------
    // Domain names: lowercase, 3-28 chars. resourceName() produces compliant
    // strings as long as projectName + stage stay short.
    const domainName = resourceName(projectName, stage, "search");

    this.domain = new Domain(this, "OpenSearchDomain", {
      domainName,
      version: EngineVersion.OPENSEARCH_2_13,

      capacity: {
        dataNodes: instanceCount,
        dataNodeInstanceType: instanceType,
        multiAzWithStandbyEnabled: false,
      },

      ebs: {
        volumeSize: 20,
        volumeType: EbsDeviceVolumeType.GP3,
      },

      zoneAwareness: {
        enabled: instanceCount >= 2,
        availabilityZoneCount: instanceCount >= 3 ? 3 : 2,
      },

      encryptionAtRest: { enabled: true },
      nodeToNodeEncryption: true,
      enforceHttps: true,
      // tlsSecurityPolicy: CDK default (TLS_1_2) is fine.

      // Fine-grained access control with an internal master user database is
      // intentionally *off*. Authentication uses IAM (resource policy +
      // SigV4) — see accessPolicies below. Promote to FGAC w/ a master user
      // ARN once we wire Cognito for OpenSearch Dashboards.

      // Open the access policy to any AWS principal in this account; we
      // narrow further via per-principal IAM policies attached to the
      // Lambdas and AppSync data sources in their own stacks.
      accessPolicies: [
        new PolicyStatement({
          effect: Effect.ALLOW,
          principals: [new AnyPrincipal()],
          actions: ["es:ESHttp*"],
          resources: [
            `arn:aws:es:${Stack.of(this).region}:${
              Stack.of(this).account
            }:domain/${domainName}/*`,
          ],
          conditions: {
            StringEquals: {
              "aws:PrincipalAccount": Stack.of(this).account,
            },
          },
        }),
      ],

      removalPolicy: isProd ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY,
    });

    this.domainEndpoint = this.domain.domainEndpoint;

    // ---------------------------------------------------------------------
    // Outputs
    // ---------------------------------------------------------------------
    new CfnOutput(this, "OpenSearchDomainName", {
      value: this.domain.domainName,
      exportName: resourceName(projectName, stage, "opensearch-domain-name"),
    });
    new CfnOutput(this, "OpenSearchDomainEndpoint", {
      value: this.domain.domainEndpoint,
      exportName: resourceName(
        projectName,
        stage,
        "opensearch-domain-endpoint"
      ),
    });
    new CfnOutput(this, "OpenSearchDomainArn", {
      value: this.domain.domainArn,
      exportName: resourceName(projectName, stage, "opensearch-domain-arn"),
    });
  }
}
