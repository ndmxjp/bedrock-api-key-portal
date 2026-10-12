import { resolve } from "node:path";
import { App } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { describe, expect, it } from "vitest";
import { validateConfig } from "../lib/config.js";
import { PortalStack } from "../lib/portal-stack.js";
import { SesIdentityStack } from "../lib/ses-stack.js";

const baseConfig = {
  allowedDomains: ["company.co.jp"],
  inferenceProfiles: [{ id: "us.example.model-v1:0", modelId: "example.model-v1:0" }],
  alarmEmail: "admin@company.co.jp",
  monthlyBudgetUsd: 50,
};

function synth(overrides: Record<string, unknown> = {}): Template {
  // Skip asset bundling in unit tests.
  const app = new App({ context: { "aws:cdk:bundling-stacks": [] } });
  const stack = new PortalStack(app, "TestStack", {
    deployNonce: "test",
    config: validateConfig({ ...baseConfig, ...overrides }),
    backendDir: resolve(import.meta.dirname, "..", "..", "backend"),
    frontendDistDir: "/nonexistent",
    skipFrontendDeployment: true,
    env: { account: "123456789012", region: "ap-northeast-1" },
  });
  return Template.fromStack(stack);
}

const template = synth();

function rolePolicyStatements(t: Template, rolePrefix: string): Array<Record<string, any>> {
  const policies = t.findResources("AWS::IAM::Policy");
  return Object.values(policies)
    .filter((p: any) => p.Properties.Roles.some((r: any) => String(r.Ref).startsWith(rolePrefix)))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
}

function issuerPolicyStatements(): Array<Record<string, any>> {
  return rolePolicyStatements(template, "ApiFnServiceRole");
}

describe("config validation", () => {
  it("rejects keys longer than the ceiling", () => {
    expect(() => validateConfig({ ...baseConfig, credentialAgeDays: 31 })).toThrow(/exceeds maxCredentialAgeDays/);
    expect(() => validateConfig({ ...baseConfig, credentialAgeDays: "unlimited" })).toThrow(/exceeds/);
    expect(() => validateConfig({ ...baseConfig, maxCredentialAgeDays: 36601 })).toThrow(/maxCredentialAgeDays/);
    expect(validateConfig({ ...baseConfig, maxCredentialAgeDays: "unlimited", credentialAgeDays: "unlimited" }).credentialAgeDays).toBe(0);
  });
  it("rejects placeholder profiles", () => {
    expect(() =>
      validateConfig({ ...baseConfig, inferenceProfiles: [{ id: "us.REPLACE_ME", modelId: "REPLACE_ME" }] }),
    ).toThrow(/placeholder/);
  });
  it("rejects the shipped example as-is", async () => {
    const example = JSON.parse(
      (await import("node:fs")).readFileSync(resolve(import.meta.dirname, "..", "portal.config.example.json"), "utf8"),
    );
    expect(example.allowedDomains).toEqual(["example.com"]);
    expect(() => validateConfig(example)).toThrow(/placeholder/);
  });
  it("requires a domain", () => {
    expect(() => validateConfig({ ...baseConfig, allowedDomains: [] })).toThrow(/allowedDomains/);
  });
  it("applies defaults", () => {
    const c = validateConfig(baseConfig);
    expect(c.credentialAgeDays).toBe(7);
    expect(c.registrationCap).toBe(10);
    expect(c.bedrockRegion).toBe("us-west-2");
  });
});

describe("Cognito", () => {
  it("uses password sign-in with Cognito's built-in email (no SES)", () => {
    template.hasResourceProperties("AWS::Cognito::UserPool", {
      UserPoolTier: "LITE",
      EmailConfiguration: Match.objectLike({ EmailSendingAccount: "COGNITO_DEFAULT" }),
      LambdaConfig: Match.objectLike({ PreSignUp: Match.anyValue() }),
      AdminCreateUserConfig: Match.objectLike({ AllowAdminCreateUserOnly: false }),
      UsernameAttributes: ["email"],
      AutoVerifiedAttributes: ["email"],
      Schema: Match.arrayWith([Match.objectLike({ Name: "email", Mutable: false, Required: true })]),
      AccountRecoverySetting: { RecoveryMechanisms: [{ Name: "verified_email", Priority: 1 }] },
      Policies: Match.objectLike({
        PasswordPolicy: Match.objectLike({ MinimumLength: 12, TemporaryPasswordValidityDays: 7 }),
      }),
    });
    const pool = Object.values(template.findResources("AWS::Cognito::UserPool"))[0] as any;
    expect(JSON.stringify(pool)).not.toMatch(/EMAIL_OTP|ses:/);
  });
  it("client allows only SRP (no plaintext password flow) and no OAuth", () => {
    template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
      ExplicitAuthFlows: ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
      AllowedOAuthFlowsUserPoolClient: false,
      GenerateSecret: false,
      PreventUserExistenceErrors: "ENABLED",
      WriteAttributes: ["email"],
    });
  });
});

describe("API", () => {
  it("every route requires the JWT authorizer", () => {
    const routes = template.findResources("AWS::ApiGatewayV2::Route");
    expect(Object.keys(routes)).toHaveLength(13);
    for (const r of Object.values(routes) as any[]) {
      expect(r.Properties.AuthorizationType).toBe("JWT");
    }
  });
  it("exposes the admin routes", () => {
    const keys = Object.values(template.findResources("AWS::ApiGatewayV2::Route")).map((r: any) => r.Properties.RouteKey);
    expect(keys).toEqual(
      expect.arrayContaining([
        "GET /api/admin/overview",
        "GET /api/admin/users",
        "POST /api/admin/users/{sub}/revoke",
        "POST /api/admin/users/{sub}/release-slot",
        "PUT /api/admin/issuance",
        "GET /api/admin/costs",
        "POST /api/admin/users/{sub}/suspend",
        "POST /api/admin/users/{sub}/resume",
        "PUT /api/admin/registration-cap",
        "PUT /api/admin/credential-age",
      ]),
    );
  });
  it("creates the admin group", () => {
    template.hasResourceProperties("AWS::Cognito::UserPoolGroup", { GroupName: "PortalAdmins" });
  });
  it("throttles key issuance", () => {
    template.hasResourceProperties("AWS::ApiGatewayV2::Stage", {
      RouteSettings: Match.objectLike({ "POST /api/keys": { ThrottlingBurstLimit: 5, ThrottlingRateLimit: 1 } }),
    });
  });
  it("stage is created after the routes it throttles", () => {
    const [stage] = Object.values(template.findResources("AWS::ApiGatewayV2::Stage")) as any[];
    const routeIds = Object.keys(template.findResources("AWS::ApiGatewayV2::Route"));
    expect(stage.DependsOn).toEqual(expect.arrayContaining(routeIds));
  });
  it("access log format contains no headers or claims", () => {
    const stage = Object.values(template.findResources("AWS::ApiGatewayV2::Stage"))[0] as any;
    const format: string = stage.Properties.AccessLogSettings.Format;
    expect(format).not.toMatch(/header|claims|querystring/i);
  });
});

describe("issuer Lambda IAM", () => {
  const statements = issuerPolicyStatements();
  const find = (action: string) =>
    statements.find((s) => s.Effect === "Allow" && [s.Action].flat().includes(action));

  it("pins service-specific credentials to Bedrock and <=30 days", () => {
    const s = find("iam:CreateServiceSpecificCredential");
    expect(s?.Condition).toEqual({
      StringEquals: { "iam:ServiceSpecificCredentialServiceName": "bedrock.amazonaws.com" },
      NumericLessThanEquals: { "iam:ServiceSpecificCredentialAgeDays": "30" },
    });
    expect(JSON.stringify(s?.Resource)).toContain("user/bedrock-poc/poc-bedrock-*");
  });
  it("requires the permissions boundary on CreateUser", () => {
    expect(find("iam:CreateUser")?.Condition?.StringEquals).toHaveProperty("iam:PermissionsBoundary");
  });
  it("can only attach the portal policy", () => {
    expect(find("iam:AttachUserPolicy")?.Condition?.ArnEquals).toHaveProperty("iam:PolicyARN");
  });
  it("has no Bedrock or access-key permissions", () => {
    for (const s of statements.filter((x) => x.Effect === "Allow")) {
      for (const a of [s.Action].flat()) {
        expect(a).not.toMatch(/^bedrock:|CreateAccessKey|CreateLoginProfile|PutUserPolicy|DeleteUser$/);
      }
    }
    const deny = statements.find((s) => s.Effect === "Deny");
    expect(deny?.Action).toEqual(
      expect.arrayContaining(["iam:CreateAccessKey", "bedrock:InvokeModel", "bedrock:CallWithBearerToken", "aws-marketplace:Subscribe"]),
    );
  });
  it("can read costs but nothing else from Cost Explorer", () => {
    const ceActions = statements.flatMap((s) => [s.Action].flat()).filter((a: string) => a.startsWith("ce:"));
    expect(ceActions).toEqual(["ce:GetCostAndUsage"]);
  });
  it("never uses a wildcard IAM resource for Allow statements", () => {
    for (const s of statements.filter((x) => x.Effect === "Allow" && /^iam:/.test([x.Action].flat()[0]))) {
      expect(JSON.stringify(s.Resource)).not.toBe('"*"');
    }
  });
});

describe("Bedrock user policy", () => {
  const managed = Object.values(template.findResources("AWS::IAM::ManagedPolicy"))[0] as any;
  const statements: any[] = managed.Properties.PolicyDocument.Statement;

  it("allows bearer-token auth and only the configured profile", () => {
    expect(statements.find((s) => s.Action === "bedrock:CallWithBearerToken")).toBeTruthy();
    const profile = statements.find((s) => s.Sid === "InvokeProfile0");
    expect(JSON.stringify(profile.Resource)).toContain("inference-profile/us.example.model-v1:0");
    expect(profile.Condition).toEqual({ StringEquals: { "aws:RequestedRegion": "us-west-2" } });
  });
  it("only reaches foundation models through the profile", () => {
    const model = statements.find((s) => s.Sid === "InvokeModelViaProfile0");
    expect(model.Condition.StringEquals).toHaveProperty("bedrock:InferenceProfileArn");
  });
  it("grants no wildcard model access or management actions", () => {
    for (const s of statements) {
      const actions = [s.Action].flat();
      expect(actions.every((a: string) => /^bedrock:(InvokeModel|InvokeModelWithResponseStream|CallWithBearerToken)$/.test(a))).toBe(true);
      if (!actions.includes("bedrock:CallWithBearerToken")) {
        expect(JSON.stringify(s.Resource)).not.toMatch(/foundation-model\/\*"|inference-profile\/\*"/);
      }
    }
  });
});

describe("hosting & ops", () => {
  it("routes /api/* through CloudFront with caching disabled and no SPA error rewrites", () => {
    const dist = Object.values(template.findResources("AWS::CloudFront::Distribution"))[0] as any;
    const cfg = dist.Properties.DistributionConfig;
    expect(cfg.CacheBehaviors[0].PathPattern).toBe("/api/*");
    expect(cfg.CustomErrorResponses).toBeUndefined();
  });
  it("creates a budget and alarms", () => {
    template.resourceCountIs("AWS::Budgets::Budget", 1);
    expect(Object.keys(template.findResources("AWS::CloudWatch::Alarm")).length).toBeGreaterThanOrEqual(5);
  });
  it("omits budget/alarms when no alarm email", () => {
    const t = synth({ alarmEmail: undefined, monthlyBudgetUsd: undefined });
    t.resourceCountIs("AWS::Budgets::Budget", 0);
    t.resourceCountIs("AWS::CloudWatch::Alarm", 0);
  });
});

describe("model agreements (deploy time)", () => {
  it("runs a custom resource for the configured models on every deploy", () => {
    const [res] = Object.values(template.findResources("AWS::CloudFormation::CustomResource")).filter(
      (r: any) => "ModelIds" in r.Properties,
    ) as any[];
    expect(res.Properties.ModelIds).toEqual(["example.model-v1:0"]);
    expect(res.Properties.DeployNonce).toBe("test");
  });
  it("only the agreement function can subscribe", () => {
    const stmts = rolePolicyStatements(template, "ModelAgreementsFnServiceRole");
    const actions = stmts.flatMap((s) => [s.Action].flat());
    expect(actions).toEqual(expect.arrayContaining(["bedrock:CreateFoundationModelAgreement", "aws-marketplace:Subscribe"]));
    expect(actions.some((a: string) => /InvokeModel|iam:/.test(a))).toBe(false);
  });
});

describe("allowAllModels", () => {
  const t = synth({ allowAllModels: true, inferenceProfiles: [] });
  const managed = Object.values(t.findResources("AWS::IAM::ManagedPolicy"))[0] as any;
  const stmts: any[] = managed.Properties.PolicyDocument.Statement;

  it("allows every profile and model, scoped to the Bedrock region", () => {
    const profile = stmts.find((s) => s.Sid === "InvokeProfilesInRegion");
    expect(JSON.stringify(profile.Resource)).toContain("arn:aws:bedrock:us-west-2:123456789012:inference-profile/*");
    expect(profile.Condition).toEqual({ StringEquals: { "aws:RequestedRegion": "us-west-2" } });
    const via = stmts.find((s) => s.Sid === "InvokeModelsViaProfile");
    expect(JSON.stringify(via.Condition.ArnLike["bedrock:InferenceProfileArn"])).toContain(
      "arn:aws:bedrock:us-west-2:123456789012:inference-profile/*",
    );
    const direct = stmts.find((s) => s.Sid === "InvokeModelsDirectInRegion");
    expect([direct.Resource].flat()).toEqual(["arn:aws:bedrock:us-west-2::foundation-model/*"]);
  });
  it("still grants only inference actions (no management, no Marketplace)", () => {
    for (const s of stmts) {
      for (const a of [s.Action].flat()) {
        expect(a).toMatch(/^bedrock:(InvokeModel|InvokeModelWithResponseStream|CallWithBearerToken)$/);
      }
    }
  });
  it("fits the managed policy size limit", () => {
    expect(JSON.stringify(managed.Properties.PolicyDocument).length).toBeLessThan(6144);
  });
  it("accepts agreements for all models and lets the API list profiles", () => {
    const [res] = Object.values(t.findResources("AWS::CloudFormation::CustomResource")).filter(
      (r: any) => "ModelIds" in r.Properties,
    ) as any[];
    expect(res.Properties.ModelIds).toEqual(["*"]);
    const api = rolePolicyStatements(t, "ApiFnServiceRole").flatMap((s) => [s.Action].flat());
    expect(api).toContain("bedrock:ListInferenceProfiles");
  });
  it("can be limited to providers", () => {
    const tp = synth({ allowAllModels: true, inferenceProfiles: [], allowedProviders: ["Anthropic", "openai"] });
    const pol = Object.values(tp.findResources("AWS::IAM::ManagedPolicy"))[0] as any;
    const st: any[] = pol.Properties.PolicyDocument.Statement;
    const res = (sid: string) => JSON.stringify(st.find((s) => s.Sid === sid).Resource);
    expect(res("InvokeProfilesInRegion")).toContain("inference-profile/*.anthropic.*");
    expect(res("InvokeProfilesInRegion")).toContain("inference-profile/*.openai.*");
    expect(res("InvokeProfilesInRegion")).not.toMatch(/inference-profile\/\*"/);
    expect(res("InvokeModelsDirectInRegion")).toContain("foundation-model/openai.*");
    expect(res("InvokeModelsViaProfile")).not.toMatch(/foundation-model\/\*"/);
    const [crRes] = Object.values(tp.findResources("AWS::CloudFormation::CustomResource")).filter(
      (r: any) => "ModelIds" in r.Properties,
    ) as any[];
    expect(crRes.Properties.Providers).toEqual(["anthropic", "openai"]);
    expect(() => validateConfig({ ...baseConfig, allowedProviders: ["anthropic"] })).toThrow(/allowAllModels/);
  });
  it("config no longer requires inferenceProfiles", () => {
    expect(validateConfig({ ...baseConfig, allowAllModels: true, inferenceProfiles: [] }).allowAllModels).toBe(true);
    expect(() => validateConfig({ ...baseConfig, inferenceProfiles: [] })).toThrow(/allowAllModels/);
  });
});

describe("IP restriction options", () => {
  it("is off by default", () => {
    template.resourceCountIs("AWS::CloudFront::Function", 0);
    template.resourceCountIs("AWS::SecretsManager::Secret", 0);
    const dist = Object.values(template.findResources("AWS::CloudFront::Distribution"))[0] as any;
    expect(dist.Properties.DistributionConfig.IPV6Enabled).toBe(true);
    const managed = Object.values(template.findResources("AWS::IAM::ManagedPolicy"))[0] as any;
    expect(JSON.stringify(managed)).not.toContain("aws:SourceIp");
  });

  it("portalAllowedCidrs filters both behaviors, disables IPv6 and verifies the origin", () => {
    const t = synth({ portalAllowedCidrs: ["203.0.113.0/24"] });
    t.resourceCountIs("AWS::CloudFront::Function", 1);
    const fn = Object.values(t.findResources("AWS::CloudFront::Function"))[0] as any;
    expect(fn.Properties.FunctionCode).toContain(JSON.stringify([[0xcb007100, 0xffffff00]]));
    const cfg = (Object.values(t.findResources("AWS::CloudFront::Distribution"))[0] as any).Properties.DistributionConfig;
    expect(cfg.IPV6Enabled).toBe(false);
    expect(cfg.DefaultCacheBehavior.FunctionAssociations[0].EventType).toBe("viewer-request");
    expect(cfg.CacheBehaviors[0].FunctionAssociations[0].EventType).toBe("viewer-request");
    const apiOrigin = cfg.Origins.find((o: any) => o.CustomOriginConfig);
    expect(apiOrigin.OriginCustomHeaders[0].HeaderName).toBe("X-Origin-Verify");
    expect(JSON.stringify(apiOrigin.OriginCustomHeaders[0].HeaderValue)).toContain("resolve:secretsmanager");
    const apiFn = Object.values(t.findResources("AWS::Lambda::Function")).find(
      (f: any) => f.Properties.Handler === "portal.handler.handler",
    ) as any;
    expect(apiFn.Properties.Environment.Variables).toHaveProperty("ORIGIN_VERIFY_SECRET_ARN");
  });

  it("keyAllowedCidrs denies Bedrock calls from other source IPs", () => {
    const t = synth({ keyAllowedCidrs: ["203.0.113.0/24", "2001:db8::/32"] });
    const managed = Object.values(t.findResources("AWS::IAM::ManagedPolicy"))[0] as any;
    const deny = managed.Properties.PolicyDocument.Statement.find((s: any) => s.Sid === "DenyOutsideAllowedSourceIps");
    expect(deny.Effect).toBe("Deny");
    expect(deny.Condition).toEqual({
      NotIpAddress: { "aws:SourceIp": ["203.0.113.0/24", "2001:db8::/32"] },
      Bool: { "aws:ViaAWSService": "false" },
    });
    expect(deny.Action).toEqual(expect.arrayContaining(["bedrock:InvokeModel", "bedrock:CallWithBearerToken"]));
  });

  it("validates CIDRs", () => {
    expect(() => validateConfig({ ...baseConfig, portalAllowedCidrs: ["2001:db8::/32"] })).toThrow(/IPv6 is not supported/);
    expect(() => validateConfig({ ...baseConfig, portalAllowedCidrs: ["10.0.0.0"] })).toThrow(/portalAllowedCidrs/);
    expect(() => validateConfig({ ...baseConfig, keyAllowedCidrs: ["nope"] })).toThrow(/keyAllowedCidrs/);
    expect(validateConfig({ ...baseConfig, keyAllowedCidrs: ["2001:db8::/32"] }).keyAllowedCidrs).toEqual(["2001:db8::/32"]);
  });
});

describe("unlimited key validity", () => {
  const createStmt = (t: Template) =>
    rolePolicyStatements(t, "ApiFnServiceRole").find(
      (s) => s.Effect === "Allow" && [s.Action].flat().includes("iam:CreateServiceSpecificCredential"),
    );
  it("keeps the age condition for a numeric ceiling", () => {
    const t = synth({ maxCredentialAgeDays: 90 });
    expect(createStmt(t)?.Condition.NumericLessThanEquals).toEqual({ "iam:ServiceSpecificCredentialAgeDays": "90" });
  });
  it("drops only the age condition when unlimited is allowed", () => {
    const t = synth({ maxCredentialAgeDays: "unlimited" });
    expect(createStmt(t)?.Condition).toEqual({
      StringEquals: { "iam:ServiceSpecificCredentialServiceName": "bedrock.amazonaws.com" },
    });
    const apiFn = Object.values(t.findResources("AWS::Lambda::Function")).find(
      (f: any) => f.Properties.Handler === "portal.handler.handler",
    ) as any;
    expect(apiFn.Properties.Environment.Variables.MAX_CREDENTIAL_AGE_DAYS).toBe("0");
  });
});

describe("restrictRegion: false", () => {
  const t = synth({ allowAllModels: true, inferenceProfiles: [], allowedProviders: ["anthropic"], restrictRegion: false });
  const stmts: any[] = (Object.values(t.findResources("AWS::IAM::ManagedPolicy"))[0] as any).Properties.PolicyDocument.Statement;
  it("drops aws:RequestedRegion and allows profiles in every region", () => {
    expect(JSON.stringify(stmts)).not.toContain("aws:RequestedRegion");
    expect(JSON.stringify(stmts.find((s) => s.Sid === "InvokeProfilesInRegion").Resource)).toContain(
      "arn:aws:bedrock:*:123456789012:inference-profile/*.anthropic.*",
    );
    expect(JSON.stringify(stmts.find((s) => s.Sid === "InvokeModelsDirectInRegion").Resource)).toContain(
      "arn:aws:bedrock:*::foundation-model/anthropic.*",
    );
  });
  it("keeps provider scoping and only inference actions", () => {
    for (const s of stmts) {
      for (const a of [s.Action].flat()) expect(a).toMatch(/^bedrock:(InvokeModel|InvokeModelWithResponseStream|CallWithBearerToken)$/);
    }
    expect(JSON.stringify(stmts)).not.toMatch(/foundation-model\/\*"/);
  });
  it("defaults to restricting the region", () => {
    expect(validateConfig(baseConfig).restrictRegion).toBe(true);
    expect(JSON.stringify(template.toJSON())).toContain("aws:RequestedRegion");
  });
});

describe("optional SES email", () => {
  it("uses Cognito's built-in sender when ses is omitted", () => {
    template.hasResourceProperties("AWS::Cognito::UserPool", {
      EmailConfiguration: Match.objectLike({ EmailSendingAccount: "COGNITO_DEFAULT" }),
    });
  });
  it("switches the user pool to SES with a verified domain", () => {
    const t = synth({ ses: { fromEmail: "no-reply@mail.example.com", verifiedDomain: "mail.example.com", fromName: "Portal" } });
    t.hasResourceProperties("AWS::Cognito::UserPool", {
      EmailConfiguration: Match.objectLike({
        EmailSendingAccount: "DEVELOPER",
        From: "Portal <no-reply@mail.example.com>",
        SourceArn: Match.objectLike({ "Fn::Join": Match.anyValue() }),
      }),
    });
  });
  it("route53 mode defaults the sender and validates the zone", () => {
    const c = validateConfig({ ...baseConfig, ses: { route53: { hostedZoneId: "Z0123456789ABC", zoneName: "poc-mail.example" } } });
    expect(c.ses?.fromEmail).toBe("no-reply@poc-mail.example");
    expect(c.ses?.verifiedDomain).toBe("poc-mail.example");
    expect(() => validateConfig({ ...baseConfig, ses: { route53: { hostedZoneId: "nope", zoneName: "x" } } })).toThrow(/route53/);
    expect(() => validateConfig({ ...baseConfig, ses: { fromEmail: "REPLACE_ME@x.example" } })).toThrow(/placeholder/);
    expect(() => validateConfig({ ...baseConfig, ses: {} })).toThrow(/fromEmail/);
  });
  it("SES identity stack publishes DKIM, MAIL FROM and DMARC", () => {
    const app = new App();
    const stack = new SesIdentityStack(app, "Ses", {
      hostedZoneId: "Z0123456789ABC",
      zoneName: "poc-mail.example",
      createDmarcRecord: true,
      env: { account: "123456789012", region: "ap-northeast-1" },
    });
    const t = Template.fromStack(stack);
    t.hasResourceProperties("AWS::SES::EmailIdentity", {
      EmailIdentity: "poc-mail.example",
      MailFromAttributes: Match.objectLike({ MailFromDomain: "mail.poc-mail.example" }),
    });
    t.hasResourceProperties("AWS::Route53::RecordSet", { Name: "_dmarc.poc-mail.example.", Type: "TXT" });
    expect(Object.keys(t.findResources("AWS::Route53::RecordSet")).length).toBe(6); // 3 DKIM + MX + SPF + DMARC
  });
});
