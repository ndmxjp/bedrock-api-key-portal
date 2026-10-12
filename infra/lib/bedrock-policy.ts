import type { PolicyStatementProps } from "aws-cdk-lib/aws-iam";
import { Effect } from "aws-cdk-lib/aws-iam";
import type { InferenceProfileConfig } from "./config.js";

const INVOKE_ACTIONS = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"];

/**
 * Least-privilege statements for PoC users.
 *
 * - The inference profile may be invoked only in the configured Bedrock region.
 * - The underlying foundation model may be reached only *through* that profile
 *   (bedrock:InferenceProfileArn), in whichever region the profile routes to. This covers
 *   both geographic and global cross-Region inference without listing destination regions.
 * - bedrock:CallWithBearerToken is required for every call made with a Bedrock API key.
 *   The action has no resource type, so it must use "*".
 * - Converse / ConverseStream are authorized by InvokeModel / InvokeModelWithResponseStream.
 */
export function bedrockUserStatements(
  profiles: InferenceProfileConfig[],
  bedrockRegion: string,
  account: string,
  restrictRegion = true,
): PolicyStatementProps[] {
  const statements: PolicyStatementProps[] = [
    {
      sid: "AllowBedrockApiKeyAuth",
      effect: Effect.ALLOW,
      actions: ["bedrock:CallWithBearerToken"],
      resources: ["*"],
    },
  ];
  profiles.forEach((p, i) => {
    const profileArn = `arn:aws:bedrock:${restrictRegion ? bedrockRegion : "*"}:${account}:inference-profile/${p.id}`;
    statements.push(
      {
        sid: `InvokeProfile${i}`,
        effect: Effect.ALLOW,
        actions: INVOKE_ACTIONS,
        resources: [profileArn],
        conditions: restrictRegion ? { StringEquals: { "aws:RequestedRegion": bedrockRegion } } : undefined,
      },
      {
        sid: `InvokeModelViaProfile${i}`,
        effect: Effect.ALLOW,
        actions: INVOKE_ACTIONS,
        resources: [
          `arn:aws:bedrock:*::foundation-model/${p.modelId}`,
          `arn:aws:bedrock:::foundation-model/${p.modelId}`,
        ],
        conditions: { [restrictRegion ? "StringEquals" : "ArnLike"]: { "bedrock:InferenceProfileArn": profileArn } },
      },
    );
  });
  return statements;
}

/**
 * "All models" mode (optionally limited to providers): any system-defined inference profile
 * in bedrockRegion, any foundation model reached through such a profile (wherever it routes),
 * and direct on-demand invocation of foundation models in bedrockRegion. Enumerating ~100 profiles by ARN would exceed the
 * 6,144-character managed policy limit, hence the region-scoped wildcards.
 * Marketplace models additionally require an account agreement (accepted at deploy time).
 */
export function allModelsStatements(
  bedrockRegion: string,
  account: string,
  providers: string[] = [],
  restrictRegion = true,
): PolicyStatementProps[] {
  // With the region unrestricted, profiles in every region are allowed and the
  // aws:RequestedRegion conditions are omitted.
  const regionPart = restrictRegion ? bedrockRegion : "*";
  const regionCondition = restrictRegion ? { StringEquals: { "aws:RequestedRegion": bedrockRegion } } : undefined;
  const profileBase = `arn:aws:bedrock:${regionPart}:${account}:inference-profile/`;
  // Profile IDs look like "<geo>.<provider>.<model>"; model IDs like "<provider>.<model>".
  const profiles = providers.length ? providers.map((p) => `${profileBase}*.${p}.*`) : [`${profileBase}*`];
  const models = (region: string) =>
    providers.length
      ? providers.map((p) => `arn:aws:bedrock:${region}::foundation-model/${p}.*`)
      : [`arn:aws:bedrock:${region}::foundation-model/*`];
  return [
    {
      sid: "AllowBedrockApiKeyAuth",
      effect: Effect.ALLOW,
      actions: ["bedrock:CallWithBearerToken"],
      resources: ["*"],
    },
    {
      sid: "InvokeProfilesInRegion",
      effect: Effect.ALLOW,
      actions: INVOKE_ACTIONS,
      resources: profiles,
      conditions: regionCondition,
    },
    {
      sid: "InvokeModelsViaProfile",
      effect: Effect.ALLOW,
      actions: INVOKE_ACTIONS,
      resources: [...models("*"), ...models("")],
      conditions: { ArnLike: { "bedrock:InferenceProfileArn": profiles } },
    },
    {
      sid: "InvokeModelsDirectInRegion",
      effect: Effect.ALLOW,
      actions: INVOKE_ACTIONS,
      resources: models(regionPart),
      conditions: regionCondition,
    },
  ];
}

/**
 * Optional: issued keys may only call Bedrock from these source IPs. A Deny in the policy that
 * is also the permissions boundary cannot be bypassed by any other grant. Calls Bedrock makes
 * on the caller's behalf (e.g. cross-Region routing) are exempt via aws:ViaAWSService.
 * Note: requests through a VPC endpoint carry aws:VpcSourceIp instead and would be denied.
 */
export function sourceIpDenyStatement(cidrs: string[]): PolicyStatementProps {
  return {
    sid: "DenyOutsideAllowedSourceIps",
    effect: Effect.DENY,
    actions: [...INVOKE_ACTIONS, "bedrock:CallWithBearerToken"],
    resources: ["*"],
    conditions: {
      NotIpAddress: { "aws:SourceIp": cidrs },
      Bool: { "aws:ViaAWSService": "false" },
    },
  };
}
