import { readFileSync } from "node:fs";
import { parseIpv4Cidr } from "./ip-filter.js";

export interface InferenceProfileConfig {
  /** Inference profile ID, e.g. "us.<provider>.<model>" or "global.<provider>.<model>". */
  readonly id: string;
  /** Underlying foundation model ID the profile routes to. */
  readonly modelId: string;
  /** Display name shown in the portal. */
  readonly label?: string;
}

export interface PortalConfig {
  readonly stackName: string;
  readonly allowedDomains: string[];
  /** Default validity of new keys in days; 0 = no expiry. Admins can change it in the portal. */
  readonly credentialAgeDays: number;
  /** Longest validity admins may choose; 0 = unlimited (keys without expiry) allowed. */
  readonly maxCredentialAgeDays: number;
  readonly registrationCap: number;
  /** Home Bedrock region: where agreements are accepted, models are listed and examples point. */
  readonly bedrockRegion: string;
  /** When false, issued keys may call Bedrock in any region (policy drops aws:RequestedRegion). */
  readonly restrictRegion: boolean;
  readonly inferenceProfiles: InferenceProfileConfig[];
  /**
   * Allow every model in bedrockRegion instead of the inferenceProfiles list. Marketplace
   * models are usable once their agreement is accepted (done automatically at deploy).
   */
  readonly allowAllModels: boolean;
  /** With allowAllModels: only these providers (model ID prefix, e.g. "anthropic"). Empty = all. */
  readonly allowedProviders: string[];
  readonly alarmEmail?: string;
  readonly monthlyBudgetUsd?: number;
  readonly removalPolicy: "destroy" | "retain";
  /** IPv4 CIDRs allowed to open the portal and call its API. Empty = no restriction. */
  readonly portalAllowedCidrs: string[];
  /** IPv4/IPv6 CIDRs from which issued API keys may call Bedrock. Empty = no restriction. */
  readonly keyAllowedCidrs: string[];
  /** Per-user minimum seconds between issuance attempts. */
  readonly issueCooldownSeconds: number;
}

/** IAM's own limit for CredentialAgeDays. */
export const IAM_MAX_CREDENTIAL_AGE_DAYS = 36600;
export const UNLIMITED = 0;

const DOMAIN_RE = /^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;
const PROFILE_ID_RE = /^[a-z0-9-]+\.[A-Za-z0-9.:\-]+$/;
const MODEL_ID_RE = /^[A-Za-z0-9.:\-]+$/;
const REGION_RE = /^[a-z]{2}(-[a-z]+)+-\d$/;

export function validateConfig(raw: unknown): PortalConfig {
  if (typeof raw !== "object" || raw === null) throw new Error("config must be an object");
  const c = raw as Record<string, unknown>;
  const errors: string[] = [];

  const allowedDomains = Array.isArray(c.allowedDomains)
    ? c.allowedDomains.map((d) => String(d).trim().toLowerCase())
    : [];
  if (allowedDomains.length === 0) errors.push("allowedDomains: at least one domain is required");
  for (const d of allowedDomains) {
    if (!DOMAIN_RE.test(d)) errors.push(`allowedDomains: invalid domain "${d}"`);
  }

  const days = (v: unknown, name: string, fallback: number): number => {
    if (v === undefined) return fallback;
    if (v === "unlimited") return UNLIMITED;
    const n = Number(v);
    if (!Number.isInteger(n) || n < 1 || n > IAM_MAX_CREDENTIAL_AGE_DAYS) {
      errors.push(`${name}: must be "unlimited" or an integer 1..${IAM_MAX_CREDENTIAL_AGE_DAYS}`);
      return fallback;
    }
    return n;
  };
  const maxCredentialAgeDays = days(c.maxCredentialAgeDays, "maxCredentialAgeDays", 30);
  const credentialAgeDays = days(c.credentialAgeDays, "credentialAgeDays", 7);
  const exceeds =
    maxCredentialAgeDays !== UNLIMITED && (credentialAgeDays === UNLIMITED || credentialAgeDays > maxCredentialAgeDays);
  if (exceeds) errors.push("credentialAgeDays: exceeds maxCredentialAgeDays");

  const registrationCap = Number(c.registrationCap ?? 10);
  if (!Number.isInteger(registrationCap) || registrationCap < 1) {
    errors.push("registrationCap: must be a positive integer");
  }

  const bedrockRegion = String(c.bedrockRegion ?? "us-west-2");
  if (!REGION_RE.test(bedrockRegion)) errors.push(`bedrockRegion: invalid region "${bedrockRegion}"`);
  const restrictRegion = c.restrictRegion !== false;

  const inferenceProfiles = Array.isArray(c.inferenceProfiles)
    ? (c.inferenceProfiles as InferenceProfileConfig[])
    : [];
  const allowAllModels = c.allowAllModels === true;
  const allowedProviders = Array.isArray(c.allowedProviders)
    ? c.allowedProviders.map((p) => String(p).trim().toLowerCase())
    : [];
  for (const p of allowedProviders) {
    if (!/^[a-z0-9-]+$/.test(p)) errors.push(`allowedProviders: invalid provider "${p}"`);
  }
  if (allowedProviders.length > 0 && !allowAllModels) {
    errors.push("allowedProviders: only applies with allowAllModels: true");
  }
  if (!allowAllModels && inferenceProfiles.length === 0) {
    errors.push("inferenceProfiles: at least one inference profile is required (or set allowAllModels)");
  }
  for (const p of inferenceProfiles) {
    if (!p || !PROFILE_ID_RE.test(String(p.id))) errors.push(`inferenceProfiles: invalid id "${p?.id}"`);
    if (!p || !MODEL_ID_RE.test(String(p.modelId))) {
      errors.push(`inferenceProfiles: invalid modelId "${p?.modelId}"`);
    }
    if (p && String(p.id).includes("REPLACE_ME")) {
      errors.push("inferenceProfiles: replace the placeholder with a real inference profile ID");
    }
  }

  if (c.alarmEmail && String(c.alarmEmail).includes("REPLACE_")) errors.push("alarmEmail: replace the placeholder");

  const removalPolicy = (c.removalPolicy ?? "destroy") as PortalConfig["removalPolicy"];
  if (removalPolicy !== "destroy" && removalPolicy !== "retain") {
    errors.push('removalPolicy: must be "destroy" or "retain"');
  }

  const monthlyBudgetUsd = c.monthlyBudgetUsd === undefined ? undefined : Number(c.monthlyBudgetUsd);
  if (monthlyBudgetUsd !== undefined && !(monthlyBudgetUsd > 0)) {
    errors.push("monthlyBudgetUsd: must be a positive number");
  }
  if (monthlyBudgetUsd !== undefined && !c.alarmEmail) {
    errors.push("alarmEmail: required when monthlyBudgetUsd is set");
  }

  const issueCooldownSeconds = Number(c.issueCooldownSeconds ?? 10);
  if (!Number.isInteger(issueCooldownSeconds) || issueCooldownSeconds < 0) {
    errors.push("issueCooldownSeconds: must be a non-negative integer");
  }

  const cidrList = (v: unknown, name: string): string[] => {
    if (v === undefined) return [];
    if (!Array.isArray(v)) {
      errors.push(`${name}: must be an array of CIDRs`);
      return [];
    }
    return v.map((x) => String(x).trim());
  };
  const portalAllowedCidrs = cidrList(c.portalAllowedCidrs, "portalAllowedCidrs");
  for (const cidr of portalAllowedCidrs) {
    try {
      parseIpv4Cidr(cidr);
    } catch {
      errors.push(`portalAllowedCidrs: invalid IPv4 CIDR "${cidr}" (IPv6 is not supported here)`);
    }
  }
  const keyAllowedCidrs = cidrList(c.keyAllowedCidrs, "keyAllowedCidrs");
  const IPV6_CIDR = /^[0-9a-fA-F:]+\/(\d{1,3})$/;
  for (const cidr of keyAllowedCidrs) {
    let ok = false;
    try {
      parseIpv4Cidr(cidr);
      ok = true;
    } catch {
      const m = IPV6_CIDR.exec(cidr);
      ok = !!m && cidr.includes(":") && Number(m[1]) <= 128;
    }
    if (!ok) errors.push(`keyAllowedCidrs: invalid CIDR "${cidr}"`);
  }

  if (errors.length > 0) throw new Error(`Invalid portal config:\n  - ${errors.join("\n  - ")}`);

  return {
    stackName: String(c.stackName ?? "BedrockApiKeyPortal"),
    allowedDomains,
    credentialAgeDays,
    maxCredentialAgeDays,
    registrationCap,
    bedrockRegion,
    restrictRegion,
    inferenceProfiles,
    allowAllModels,
    allowedProviders,
    alarmEmail: c.alarmEmail ? String(c.alarmEmail) : undefined,
    monthlyBudgetUsd,
    removalPolicy,
    issueCooldownSeconds,
    portalAllowedCidrs,
    keyAllowedCidrs,
  };
}

export function loadConfig(path: string): PortalConfig {
  let text: string;
  try {
    text = readFileSync(path, "utf8");
  } catch {
    throw new Error(
      `Config file not found: ${path}\nCopy infra/portal.config.example.json to ${path} and edit it.`,
    );
  }
  return validateConfig(JSON.parse(text));
}
