import { CfnOutput, Duration, Stack, type StackProps } from "aws-cdk-lib";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as ses from "aws-cdk-lib/aws-ses";
import type { Construct } from "constructs";

export interface SesIdentityStackProps extends StackProps {
  /** Public hosted zone (in this account) whose apex domain becomes the SES sender domain. */
  readonly hostedZoneId: string;
  readonly zoneName: string;
  /** Publish a monitoring-only DMARC record (p=none). Skip if the domain already has one. */
  readonly createDmarcRecord: boolean;
}

/**
 * Optional: SES domain identity with Easy DKIM and a custom MAIL FROM, published to Route 53.
 * Deploy this first and wait until the identity is "Verified" before deploying the portal
 * with `ses.route53` set: Cognito refuses unverified SES identities.
 */
export class SesIdentityStack extends Stack {
  constructor(scope: Construct, id: string, props: SesIdentityStackProps) {
    super(scope, id, props);
    const zone = route53.PublicHostedZone.fromPublicHostedZoneAttributes(this, "Zone", {
      hostedZoneId: props.hostedZoneId,
      zoneName: props.zoneName,
    });

    // DKIM (d=<domain>) and SPF via MAIL FROM (<mail.domain>) both align with the From domain,
    // which corporate mail filters typically require.
    new ses.EmailIdentity(this, "DomainIdentity", {
      identity: ses.Identity.publicHostedZone(zone),
      mailFromDomain: `mail.${props.zoneName}`,
      mailFromBehaviorOnMxFailure: ses.MailFromBehaviorOnMxFailure.USE_DEFAULT_VALUE,
    });

    if (props.createDmarcRecord) {
      new route53.TxtRecord(this, "Dmarc", {
        zone,
        recordName: `_dmarc.${props.zoneName}`,
        values: ["v=DMARC1; p=none;"],
        ttl: Duration.hours(1),
      });
    }

    new CfnOutput(this, "SesDomain", { value: props.zoneName });
    new CfnOutput(this, "CheckVerification", {
      value: `aws sesv2 get-email-identity --email-identity ${props.zoneName} --region ${this.region} --query '{verified:VerifiedForSendingStatus,dkim:DkimAttributes.Status}'`,
    });
  }
}
