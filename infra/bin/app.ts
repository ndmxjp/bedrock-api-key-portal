import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { App } from "aws-cdk-lib";
import { loadConfig } from "../lib/config.js";
import { PortalStack } from "../lib/portal-stack.js";
import { SesIdentityStack } from "../lib/ses-stack.js";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..", "..");
const app = new App();

const configPath = resolve(
  here,
  "..",
  (app.node.tryGetContext("config") as string | undefined) ?? process.env.PORTAL_CONFIG ?? "portal.config.json",
);
const config = loadConfig(configPath);

const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION };

const r53 = config.ses?.route53;
if (r53) {
  // Deploy first (`make deploy-ses`), then wait until the identity is verified.
  new SesIdentityStack(app, `${config.stackName}-SesIdentity`, {
    hostedZoneId: r53.hostedZoneId,
    zoneName: r53.zoneName,
    createDmarcRecord: r53.createDmarcRecord ?? true,
    env: { account: env.account, region: config.ses?.sesRegion ?? env.region },
    description: "SES sender domain identity for the Bedrock API key portal (PoC)",
    tags: { Project: "BedrockApiKeyPortal", PoC: "true" },
  });
}

new PortalStack(app, config.stackName, {
  config,
  backendDir: resolve(root, "backend"),
  frontendDistDir: resolve(root, "frontend", "dist"),
  skipFrontendDeployment: app.node.tryGetContext("skipFrontend") === "true",
  env,
  description: "Bedrock API Key Self-Service Portal (PoC)",
  tags: { Project: "BedrockApiKeyPortal", PoC: "true" },
});
