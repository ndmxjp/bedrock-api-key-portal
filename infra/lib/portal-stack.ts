import { existsSync } from "node:fs";
import { join } from "node:path";
import {
  CfnOutput,
  CustomResource,
  Duration,
  Fn,
  RemovalPolicy,
  Stack,
  type StackProps,
} from "aws-cdk-lib";
import * as apigwv2 from "aws-cdk-lib/aws-apigatewayv2";
import { HttpUserPoolAuthorizer } from "aws-cdk-lib/aws-apigatewayv2-authorizers";
import { HttpLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as budgets from "aws-cdk-lib/aws-budgets";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cwActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as sns from "aws-cdk-lib/aws-sns";
import * as subs from "aws-cdk-lib/aws-sns-subscriptions";
import * as cr from "aws-cdk-lib/custom-resources";
import type { Construct } from "constructs";
import { allModelsStatements, bedrockUserStatements, sourceIpDenyStatement } from "./bedrock-policy.js";
import { ipFilterFunctionCode } from "./ip-filter.js";
import { type PortalConfig, UNLIMITED } from "./config.js";
import { pythonCode } from "./python-asset.js";

export const IAM_USER_PATH = "/bedrock-poc/";
export const IAM_USER_PREFIX = "poc-bedrock-";
export const METRIC_NAMESPACE = "BedrockPoCPortal";
export const ADMIN_GROUP = "PortalAdmins";

export interface PortalStackProps extends StackProps {
  readonly config: PortalConfig;
  readonly backendDir: string;
  readonly frontendDistDir: string;
  /** Skip uploading the SPA (unit tests / synth without a frontend build). */
  readonly skipFrontendDeployment?: boolean;
  /** Fixed value for deterministic tests; defaults to the synth time. */
  readonly deployNonce?: string;
}

export class PortalStack extends Stack {
  constructor(scope: Construct, id: string, props: PortalStackProps) {
    super(scope, id, props);
    const { config } = props;
    const removalPolicy = config.removalPolicy === "retain" ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY;
    const runtime = lambda.Runtime.PYTHON_3_14;
    const code = pythonCode(props.backendDir, runtime);

    // ---------------------------------------------------------------- DynamoDB
    const table = new dynamodb.TableV2(this, "Registrations", {
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy,
    });

    // ---------------------------------------------------------------- Bedrock user policy
    // Attached to every PoC IAM user AND used as their permissions boundary.
    const userPolicy = new iam.ManagedPolicy(this, "BedrockPoCUserPolicy", {
      description: "Bedrock inference permissions for PoC portal users (restricted models/region).",
      statements: [
        ...(config.allowAllModels
          ? allModelsStatements(config.bedrockRegion, this.account, config.allowedProviders, config.restrictRegion)
          : bedrockUserStatements(config.inferenceProfiles, config.bedrockRegion, this.account, config.restrictRegion)),
        ...(config.keyAllowedCidrs.length ? [sourceIpDenyStatement(config.keyAllowedCidrs)] : []),
      ].map((s) => new iam.PolicyStatement(s)),
    });

    // ---------------------------------------------------------------- Model agreements
    // Accept the Marketplace agreement for target models on every deploy, so users' keys
    // (which have no Marketplace permissions) can call them. Only this deploy-time function
    // can subscribe; agreements are not cancelled when the stack is deleted.
    const agreementsFn = new lambda.Function(this, "ModelAgreementsFn", {
      runtime,
      architecture: lambda.Architecture.ARM_64,
      code,
      handler: "portal.agreements.handler",
      timeout: Duration.minutes(14),
      memorySize: 256,
      environment: { BEDROCK_REGION: config.bedrockRegion },
      logGroup: new logs.LogGroup(this, "ModelAgreementsLogs", { retention: logs.RetentionDays.ONE_MONTH, removalPolicy }),
    });
    agreementsFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "AcceptModelAgreements",
        actions: [
          "bedrock:ListFoundationModels",
          "bedrock:GetFoundationModelAvailability",
          "bedrock:ListFoundationModelAgreementOffers",
          "bedrock:CreateFoundationModelAgreement",
          "aws-marketplace:Subscribe",
          "aws-marketplace:ViewSubscriptions",
        ],
        resources: ["*"], // these actions have no resource-level permissions
      }),
    );
    const agreementsProvider = new cr.Provider(this, "ModelAgreementsProvider", {
      onEventHandler: agreementsFn,
      logGroup: new logs.LogGroup(this, "ModelAgreementsProviderLogs", {
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy,
      }),
    });
    const agreements = new CustomResource(this, "ModelAgreements", {
      serviceToken: agreementsProvider.serviceToken,
      properties: {
        ModelIds: config.allowAllModels ? ["*"] : [...new Set(config.inferenceProfiles.map((p) => p.modelId))],
        Providers: config.allowedProviders,
        // Changes every synth so the agreements are (re)checked on every deploy.
        DeployNonce: props.deployNonce ?? new Date().toISOString(),
      },
    });
    new CfnOutput(this, "ModelAgreementsSummary", { value: agreements.getAttString("Summary") });

    // ---------------------------------------------------------------- Cognito
    const preSignUpFn = new lambda.Function(this, "PreSignUpFn", {
      runtime,
      architecture: lambda.Architecture.ARM_64,
      code,
      handler: "portal.presignup.handler",
      timeout: Duration.seconds(5),
      memorySize: 128,
      environment: { ALLOWED_DOMAINS: config.allowedDomains.join(",") },
      logGroup: new logs.LogGroup(this, "PreSignUpLogs", { retention: logs.RetentionDays.ONE_MONTH, removalPolicy }),
    });

    const userPool = new cognito.UserPool(this, "UserPool", {
      // Password sign-in only needs the Lite plan.
      featurePlan: cognito.FeaturePlan.LITE,
      // Self sign-up is open, but the Pre sign-up trigger rejects any email outside
      // allowedDomains, and the API re-checks the domain. Admins can also invite users
      // (scripts/invite_users.py). NOTE: account security automation may disable self sign-up.
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      signInCaseSensitive: false,
      autoVerify: { email: true },
      standardAttributes: { email: { required: true, mutable: false } },
      passwordPolicy: {
        minLength: 12,
        requireDigits: true,
        requireLowercase: true,
        requireUppercase: true,
        requireSymbols: false,
        tempPasswordValidity: Duration.days(7),
      },
      // "Forgot password" sends a reset code to the (admin-verified) email.
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      mfa: cognito.Mfa.OFF,
      // Cognito's built-in email (no SES). Limited daily volume: see README.
      email: cognito.UserPoolEmail.withCognito(),
      userInvitation: {
        emailSubject: "Bedrock PoC ポータルへの招待",
        emailBody:
          "Bedrock PoC ポータルの参加者として登録されました。<br>" +
          "ユーザー名: {username}<br>初期パスワード: {####}<br><br>" +
          "管理者から案内されたポータルURLでログインし、新しいパスワードを設定してください。" +
          "初期パスワードの有効期限は7日間です。",
      },
      userVerification: {
        emailStyle: cognito.VerificationEmailStyle.CODE,
        emailSubject: "Bedrock PoC ポータル 確認コード",
        emailBody: "Bedrock PoC ポータルの確認コードは {####} です。心当たりがない場合は破棄してください。",
      },
      lambdaTriggers: { preSignUp: preSignUpFn },
      deletionProtection: config.removalPolicy === "retain",
      removalPolicy,
    });

    const userPoolClient = userPool.addClient("WebClient", {
      authFlows: { userSrp: true }, // ALLOW_USER_SRP_AUTH: the password never leaves the browser
      disableOAuth: true,
      generateSecret: false,
      preventUserExistenceErrors: true,
      enableTokenRevocation: true,
      idTokenValidity: Duration.hours(1),
      accessTokenValidity: Duration.hours(1),
      refreshTokenValidity: Duration.hours(12),
      readAttributes: new cognito.ClientAttributes().withStandardAttributes({ email: true, emailVerified: true }),
      // email is needed for SignUp; it is immutable afterwards (mutable: false).
      writeAttributes: new cognito.ClientAttributes().withStandardAttributes({ email: true }),
    });

    // Members get admin API access (still subject to the domain / email-verified checks).
    // Membership is granted only out-of-band (scripts/portal_admin.py add-admin).
    new cognito.CfnUserPoolGroup(this, "AdminGroup", {
      userPoolId: userPool.userPoolId,
      groupName: ADMIN_GROUP,
      description: "Bedrock PoC portal administrators",
    });

    const tokenIssuer = `https://cognito-idp.${this.region}.amazonaws.com/${userPool.userPoolId}`;

    // ---------------------------------------------------------------- API Lambda
    const apiLogGroup = new logs.LogGroup(this, "ApiFnLogs", {
      retention: logs.RetentionDays.THREE_MONTHS,
      removalPolicy,
    });
    const apiFn = new lambda.Function(this, "ApiFn", {
      runtime,
      architecture: lambda.Architecture.ARM_64,
      code,
      handler: "portal.handler.handler",
      // Must stay below ISSUING_LEASE_SECONDS so a live attempt is never taken over.
      timeout: Duration.seconds(25),
      memorySize: 256,
      logGroup: apiLogGroup,
      environment: {
        TABLE_NAME: table.tableName,
        ALLOWED_DOMAINS: config.allowedDomains.join(","),
        CREDENTIAL_AGE_DAYS: String(config.credentialAgeDays),
        MAX_CREDENTIAL_AGE_DAYS: String(config.maxCredentialAgeDays),
        REGISTRATION_CAP: String(config.registrationCap),
        BEDROCK_REGION: config.bedrockRegion,
        RESTRICT_REGION: config.restrictRegion ? "1" : "0",
        IAM_PATH: IAM_USER_PATH,
        USER_POLICY_ARN: userPolicy.managedPolicyArn,
        PERMISSIONS_BOUNDARY_ARN: userPolicy.managedPolicyArn,
        USER_POOL_CLIENT_ID: userPoolClient.userPoolClientId,
        TOKEN_ISSUER: tokenIssuer,
        ALLOWED_MODELS: JSON.stringify(config.inferenceProfiles.map((p) => p.id)),
        MODEL_MODE: config.allowAllModels ? "all" : "list",
        ALLOWED_PROVIDERS: JSON.stringify(config.allowedProviders),
        ISSUE_COOLDOWN_SECONDS: String(config.issueCooldownSeconds),
        ISSUING_LEASE_SECONDS: "90",
        ADMIN_GROUP: ADMIN_GROUP,
      },
    });
    table.grantReadWriteData(apiFn);
    if (config.allowAllModels) {
      // Read-only: lists inference profiles to show users which models they can call.
      apiFn.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "ListModels",
          actions: ["bedrock:ListInferenceProfiles", "bedrock:ListFoundationModels"],
          resources: ["*"],
        }),
      );
    }
    // Month-to-date cost per portal user (IAM principal cost allocation). Cost Explorer has
    // no resource-level permissions, so "*" is required. Read-only.
    apiFn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "ReadPortalCosts",
        actions: ["ce:GetCostAndUsage"],
        resources: ["*"],
      }),
    );
    this.grantIssuerPermissions(apiFn, userPolicy.managedPolicyArn, config.maxCredentialAgeDays);

    // ---------------------------------------------------------------- HTTP API
    const httpApi = new apigwv2.HttpApi(this, "HttpApi", {
      description: "Bedrock API key portal",
      createDefaultStage: true,
    });
    const authorizer = new HttpUserPoolAuthorizer("CognitoAuthorizer", userPool, {
      userPoolClients: [userPoolClient], // audience = client id (ID tokens)
      identitySource: ["$request.header.Authorization"],
    });
    const integration = new HttpLambdaIntegration("ApiIntegration", apiFn, {
      payloadFormatVersion: apigwv2.PayloadFormatVersion.VERSION_2_0,
    });
    const routes: apigwv2.HttpRoute[] = [];
    for (const [path, method] of [
      ["/api/me", apigwv2.HttpMethod.GET],
      ["/api/keys", apigwv2.HttpMethod.POST],
      ["/api/keys/revoke", apigwv2.HttpMethod.POST],
      ["/api/admin/overview", apigwv2.HttpMethod.GET],
      ["/api/admin/users", apigwv2.HttpMethod.GET],
      ["/api/admin/users/{sub}/revoke", apigwv2.HttpMethod.POST],
      ["/api/admin/users/{sub}/release-slot", apigwv2.HttpMethod.POST],
      ["/api/admin/issuance", apigwv2.HttpMethod.PUT],
      ["/api/admin/costs", apigwv2.HttpMethod.GET],
      ["/api/admin/users/{sub}/suspend", apigwv2.HttpMethod.POST],
      ["/api/admin/users/{sub}/resume", apigwv2.HttpMethod.POST],
      ["/api/admin/registration-cap", apigwv2.HttpMethod.PUT],
      ["/api/admin/credential-age", apigwv2.HttpMethod.PUT],
    ] as const) {
      routes.push(...httpApi.addRoutes({ path, methods: [method], integration, authorizer }));
    }

    const accessLogs = new logs.LogGroup(this, "HttpApiAccessLogs", {
      retention: logs.RetentionDays.THREE_MONTHS,
      removalPolicy,
    });
    const stage = httpApi.defaultStage?.node.defaultChild as apigwv2.CfnStage;
    // RouteSettings must reference existing routes, so the stage has to be created after them.
    for (const route of routes) stage.node.addDependency(route);
    // No headers, query strings or authorizer claims are logged.
    stage.accessLogSettings = {
      destinationArn: accessLogs.logGroupArn,
      format: JSON.stringify({
        requestId: "$context.requestId",
        time: "$context.requestTime",
        routeKey: "$context.routeKey",
        status: "$context.status",
        ip: "$context.identity.sourceIp",
        latencyMs: "$context.responseLatency",
        authorizerError: "$context.authorizer.error",
      }),
    };
    stage.defaultRouteSettings = { throttlingBurstLimit: 20, throttlingRateLimit: 10 };
    // RouteSettings is an untyped JSON map, so CDK does not convert key casing for us.
    stage.addPropertyOverride("RouteSettings", {
      "POST /api/keys": { ThrottlingBurstLimit: 5, ThrottlingRateLimit: 1 },
      "POST /api/keys/revoke": { ThrottlingBurstLimit: 5, ThrottlingRateLimit: 2 },
    });

    // ---------------------------------------------------------------- Frontend hosting
    const siteBucket = new s3.Bucket(this, "SiteBucket", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy,
      autoDeleteObjects: removalPolicy === RemovalPolicy.DESTROY,
    });

    const cognitoEndpoint = `https://cognito-idp.${this.region}.amazonaws.com`;
    const headersPolicy = new cloudfront.ResponseHeadersPolicy(this, "SecurityHeaders", {
      securityHeadersBehavior: {
        contentSecurityPolicy: {
          override: true,
          contentSecurityPolicy: [
            "default-src 'self'",
            `connect-src 'self' ${cognitoEndpoint}`,
            "img-src 'self' data:",
            "style-src 'self'",
            "script-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
          ].join("; "),
        },
        strictTransportSecurity: {
          override: true,
          accessControlMaxAge: Duration.days(365),
          includeSubdomains: true,
        },
        contentTypeOptions: { override: true },
        frameOptions: { override: true, frameOption: cloudfront.HeadersFrameOption.DENY },
        referrerPolicy: { override: true, referrerPolicy: cloudfront.HeadersReferrerPolicy.NO_REFERRER },
      },
    });

    // ---------------------------------------------------------------- Optional IP restriction
    const ipRestricted = config.portalAllowedCidrs.length > 0;
    let ipFilter: cloudfront.Function | undefined;
    let originSecret: secretsmanager.Secret | undefined;
    if (ipRestricted) {
      ipFilter = new cloudfront.Function(this, "IpFilter", {
        runtime: cloudfront.FunctionRuntime.JS_2_0,
        code: cloudfront.FunctionCode.fromInline(ipFilterFunctionCode(config.portalAllowedCidrs)),
        comment: "Allow only configured IPv4 CIDRs",
      });
      // CloudFront sends this to API Gateway; the Lambda rejects requests without it, so the
      // execute-api URL cannot be used to get around the IP filter.
      originSecret = new secretsmanager.Secret(this, "OriginVerifySecret", {
        description: "Shared secret between CloudFront and the portal API (origin verification)",
        generateSecretString: { passwordLength: 48, excludePunctuation: true },
        removalPolicy,
      });
      originSecret.grantRead(apiFn);
      apiFn.addEnvironment("ORIGIN_VERIFY_SECRET_ARN", originSecret.secretArn);
    }
    const viewerFunctions = ipFilter
      ? [{ function: ipFilter, eventType: cloudfront.FunctionEventType.VIEWER_REQUEST }]
      : undefined;

    const apiDomain = Fn.select(2, Fn.split("/", httpApi.apiEndpoint));
    const distribution = new cloudfront.Distribution(this, "Distribution", {
      comment: "Bedrock API key portal",
      defaultRootObject: "index.html",
      // The IP filter is IPv4-only; keep viewers on IPv4 while it is enabled.
      enableIpv6: !ipRestricted,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(siteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        responseHeadersPolicy: headersPolicy,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        functionAssociations: viewerFunctions,
      },
      additionalBehaviors: {
        // Same-origin API: no CORS needed; browsers on other origins get no CORS headers.
        "/api/*": {
          origin: new origins.HttpOrigin(apiDomain, {
            protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
            customHeaders: originSecret
              ? { "X-Origin-Verify": originSecret.secretValue.unsafeUnwrap() }
              : undefined,
          }),
          functionAssociations: viewerFunctions,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          // Forwards Authorization (allowed because caching is disabled), not Host.
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        },
      },
      // NOTE: no errorResponses — they would also rewrite the API's 403/404 JSON errors.
    });

    if (!props.skipFrontendDeployment) {
      if (!existsSync(props.frontendDistDir)) {
        throw new Error(
          `Frontend build not found at ${props.frontendDistDir}. Run "npm run build" in frontend/ first.`,
        );
      }
      // Fonts are large and content-stable: cache them for a year, separately from the app files.
      // Deployed first and pruned from the main deployment so the two never fight over the keys.
      const fontsDeployment = new s3deploy.BucketDeployment(this, "DeployFonts", {
        destinationBucket: siteBucket,
        destinationKeyPrefix: "fonts",
        sources: [s3deploy.Source.asset(join(props.frontendDistDir, "fonts"))],
        cacheControl: [s3deploy.CacheControl.maxAge(Duration.days(365)), s3deploy.CacheControl.immutable()],
        // S3 would otherwise guess binary/octet-stream; with X-Content-Type-Options: nosniff the
        // real type matters. Only .woff2 files are deployed here (the license is excluded).
        contentType: "font/woff2",
        exclude: ["*.txt"],
        prune: true,
        memoryLimit: 256,
      });
      const siteDeployment = new s3deploy.BucketDeployment(this, "DeploySite", {
        destinationBucket: siteBucket,
        exclude: ["fonts/*"],
        sources: [
          s3deploy.Source.asset(props.frontendDistDir, { exclude: ["fonts", "fonts/**"] }),
          s3deploy.Source.jsonData("config.json", {
            region: this.region,
            userPoolId: userPool.userPoolId,
            userPoolClientId: userPoolClient.userPoolClientId,
            // UX hint only; the Pre sign-up trigger and the API enforce the domain.
            allowedDomains: config.allowedDomains,
          }),
        ],
        cacheControl: [s3deploy.CacheControl.noCache()],
        distribution,
        distributionPaths: ["/*"],
        memoryLimit: 256,
      });
      siteDeployment.node.addDependency(fontsDeployment);
    }

    // ---------------------------------------------------------------- Monitoring
    if (config.alarmEmail) {
      const topic = new sns.Topic(this, "AlarmTopic", { displayName: "Bedrock PoC Portal alarms" });
      topic.addSubscription(new subs.EmailSubscription(config.alarmEmail));
      const action = new cwActions.SnsAction(topic);
      const portalMetric = (metricName: string) =>
        new cloudwatch.Metric({
          namespace: METRIC_NAMESPACE,
          metricName,
          statistic: "Sum",
          period: Duration.hours(1),
        });

      const alarms: cloudwatch.Alarm[] = [
        apiFn.metricErrors({ period: Duration.minutes(5) }).createAlarm(this, "ApiFnErrors", {
          threshold: 1,
          evaluationPeriods: 1,
          alarmDescription: "Portal API Lambda errors",
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        }),
        portalMetric("IssueFailed").createAlarm(this, "IssueFailures", {
          threshold: 1,
          evaluationPeriods: 1,
          alarmDescription: "API key issuance failed (IAM/DynamoDB)",
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        }),
        portalMetric("IamAccessDenied").createAlarm(this, "IamAccessDenied", {
          threshold: 1,
          evaluationPeriods: 1,
          alarmDescription: "Issuer Lambda got AccessDenied from IAM",
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        }),
        portalMetric("KeyIssued").createAlarm(this, "UnusualIssuance", {
          threshold: Math.max(3, Math.ceil(config.registrationCap / 2)),
          comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
          evaluationPeriods: 1,
          alarmDescription: "Unusually many API keys issued within one hour",
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        }),
        new cloudwatch.Metric({
          namespace: "AWS/ApiGateway",
          metricName: "5xx",
          dimensionsMap: { ApiId: httpApi.apiId, Stage: "$default" },
          statistic: "Sum",
          period: Duration.minutes(5),
        }).createAlarm(this, "Api5xx", {
          threshold: 5,
          evaluationPeriods: 1,
          alarmDescription: "HTTP API 5xx responses",
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        }),
      ];
      for (const alarm of alarms) alarm.addAlarmAction(action);

      if (config.monthlyBudgetUsd) {
        const subscriber = { subscriptionType: "EMAIL", address: config.alarmEmail };
        new budgets.CfnBudget(this, "MonthlyBudget", {
          budget: {
            budgetName: `${this.stackName}-monthly`,
            budgetType: "COST",
            timeUnit: "MONTHLY",
            budgetLimit: { amount: config.monthlyBudgetUsd, unit: "USD" },
          },
          // Budgets only notify; they never stop Bedrock usage.
          notificationsWithSubscribers: [
            {
              notification: { notificationType: "ACTUAL", comparisonOperator: "GREATER_THAN", threshold: 80 },
              subscribers: [subscriber],
            },
            {
              notification: { notificationType: "FORECASTED", comparisonOperator: "GREATER_THAN", threshold: 100 },
              subscribers: [subscriber],
            },
          ],
        });
      }
    }

    // ---------------------------------------------------------------- Outputs
    new CfnOutput(this, "PortalUrl", { value: `https://${distribution.distributionDomainName}` });
    new CfnOutput(this, "UserPoolId", { value: userPool.userPoolId });
    new CfnOutput(this, "UserPoolClientId", { value: userPoolClient.userPoolClientId });
    new CfnOutput(this, "TableName", { value: table.tableName });
    new CfnOutput(this, "UserPolicyArn", { value: userPolicy.managedPolicyArn });
    new CfnOutput(this, "ApiFunctionName", { value: apiFn.functionName });
    new CfnOutput(this, "BedrockRegion", { value: config.bedrockRegion });
  }

  /**
   * Least-privilege IAM permissions for the issuer Lambda. Scoped to users under
   * IAM_USER_PATH with the portal prefix; policy/boundary/service/age pinned by condition keys.
   * The Lambda has NO Bedrock inference permissions.
   */
  private grantIssuerPermissions(fn: lambda.Function, userPolicyArn: string, maxAgeDays: number): void {
    const managedUsers = `arn:aws:iam::${this.account}:user${IAM_USER_PATH}${IAM_USER_PREFIX}*`;
    // GetUser on a not-yet-existing user is evaluated without the path.
    const unpathedUsers = `arn:aws:iam::${this.account}:user/${IAM_USER_PREFIX}*`;

    fn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "ReadManagedUsers",
        actions: ["iam:GetUser"],
        resources: [managedUsers, unpathedUsers],
      }),
    );
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "CreateManagedUsersWithBoundary",
        actions: ["iam:CreateUser"],
        resources: [managedUsers],
        conditions: { StringEquals: { "iam:PermissionsBoundary": userPolicyArn } },
      }),
    );
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "TagManagedUsersOnCreate",
        actions: ["iam:TagUser"],
        resources: [managedUsers],
        conditions: {
          "ForAllValues:StringEquals": { "aws:TagKeys": ["ManagedBy", "PoC", "PortalSubjectHash"] },
        },
      }),
    );
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "AttachOnlyPortalPolicy",
        actions: ["iam:AttachUserPolicy"],
        resources: [managedUsers],
        conditions: {
          ArnEquals: { "iam:PolicyARN": userPolicyArn },
          StringEquals: { "iam:PermissionsBoundary": userPolicyArn },
        },
      }),
    );
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "CreateBedrockKeysOnly",
        actions: ["iam:CreateServiceSpecificCredential"],
        resources: [managedUsers],
        conditions: {
          StringEquals: { "iam:ServiceSpecificCredentialServiceName": "bedrock.amazonaws.com" },
          // With an unlimited ceiling, keys are created without CredentialAgeDays, so the age
          // condition key is absent and must not be required.
          ...(maxAgeDays === UNLIMITED
            ? {}
            : { NumericLessThanEquals: { "iam:ServiceSpecificCredentialAgeDays": String(maxAgeDays) } }),
        },
      }),
    );
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "ManageKeysOfManagedUsers",
        actions: [
          "iam:ListServiceSpecificCredentials",
          "iam:UpdateServiceSpecificCredential",
          "iam:DeleteServiceSpecificCredential",
        ],
        resources: [managedUsers],
      }),
    );
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "DenyPrivilegeEscalation",
        effect: iam.Effect.DENY,
        actions: [
          "iam:CreateAccessKey",
          "iam:CreateLoginProfile",
          "iam:UpdateLoginProfile",
          "iam:PutUserPolicy",
          "iam:DeleteUser",
          "iam:DeleteUserPermissionsBoundary",
          "iam:PutUserPermissionsBoundary",
          "iam:DetachUserPolicy",
          "iam:AddUserToGroup",
          "iam:CreateVirtualMFADevice",
          // No inference or agreement actions for the issuer itself.
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:CallWithBearerToken",
          "bedrock:CreateFoundationModelAgreement",
          "aws-marketplace:Subscribe",
        ],
        resources: ["*"],
      }),
    );
  }
}
