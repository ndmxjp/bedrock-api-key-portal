import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { App } from "aws-cdk-lib";
import { loadConfig } from "../lib/config.js";
import { PortalStack } from "../lib/portal-stack.js";

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

new PortalStack(app, config.stackName, {
  config,
  backendDir: resolve(root, "backend"),
  frontendDistDir: resolve(root, "frontend", "dist"),
  skipFrontendDeployment: app.node.tryGetContext("skipFrontend") === "true",
  env,
  description: "Bedrock API Key Self-Service Portal (PoC)",
  tags: { Project: "BedrockApiKeyPortal", PoC: "true" },
});
