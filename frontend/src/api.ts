import { getIdToken } from "./auth";

export type KeyStatus = "NOT_ISSUED" | "ISSUING" | "ACTIVE" | "SUSPENDED" | "REVOKED" | "EXPIRED" | "FAILED";

export interface KeyMetadata {
  credentialId: string;
  status: KeyStatus;
  issuedAt: string | null;
  expiresAt: string | null;
}

export interface Me {
  email: string;
  emailVerified: boolean;
  allowed: boolean;
  status: KeyStatus;
  key: KeyMetadata | null;
  region: string;
  /** false: keys work against Bedrock in any region (region is then just the example/home region) */
  regionRestricted?: boolean;
  allowedModels: string[];
  allModels?: boolean;
  modelProviders?: string[];
  /** null = keys are issued without expiry */
  credentialAgeDays: number | null;
  issuanceEnabled: boolean;
  isAdmin: boolean;
  suspended: boolean;
  usage: Usage | null;
}

export interface Usage {
  month: string;
  amount: string | null;
  currency: string;
  fetchedAt: string | null;
  available: boolean;
}

export interface AdminCosts extends Usage {
  users: { sub: string; email: string | null; amount: string }[];
  unmatchedAmount: string;
}

export interface AdminUser {
  sub: string;
  email: string | null;
  status: KeyStatus;
  credentialId: string | null;
  issuedAt: string | null;
  expiresAt: string | null;
  updatedAt: string | null;
  slotReserved: boolean;
  suspended: boolean;
  errorCode: string | null;
}

export interface AdminOverview {
  issuanceEnabled: boolean;
  reservedSlots: number;
  registrationCap: number;
  maxRegistrationCap: number;
  /** null = admins may choose "no expiry" */
  maxCredentialAgeDays: number | null;
  /** null = keys are issued without expiry */
  credentialAgeDays: number | null;
  region: string;
  allowedModels: string[];
  allowedDomains: string[];
  statusCounts: Partial<Record<KeyStatus, number>>;
}

export interface IssuedKey {
  status: "ACTIVE";
  apiKey: string;
  expiresAt: string | null;
  issuedAt: string | null;
  region: string;
  credentialId: string;
  allowedModels: string[];
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function call<T>(method: "GET" | "POST" | "PUT", path: string, body?: unknown): Promise<T> {
  const token = await getIdToken();
  if (!token) throw new ApiError(401, "UNAUTHENTICATED", "認証の有効期限が切れました。再度ログインしてください。");
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      credentials: "omit",
    });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "通信に失敗しました。ネットワーク接続を確認してください。");
  }
  const payload = (await res.json().catch(() => null)) as
    | (T & { error?: undefined })
    | { error: { code: string; message: string } }
    | null;
  if (!res.ok || !payload || payload.error) {
    const err = payload?.error;
    if (res.status === 401) {
      throw new ApiError(401, err?.code ?? "UNAUTHENTICATED", "認証の有効期限が切れました。再度ログインしてください。");
    }
    throw new ApiError(res.status, err?.code ?? "UNKNOWN", err?.message ?? `エラーが発生しました（HTTP ${res.status}）。`);
  }
  return payload as T;
}

export const getMe = () => call<Me>("GET", "/api/me");
export const issueKey = () => call<IssuedKey>("POST", "/api/keys");
export const revokeKey = () => call<Me>("POST", "/api/keys/revoke");

export const getAdminOverview = () => call<AdminOverview>("GET", "/api/admin/overview");
export const getAdminUsers = () => call<{ users: AdminUser[] }>("GET", "/api/admin/users");
export const adminRevoke = (sub: string) =>
  call<{ user: AdminUser }>("POST", `/api/admin/users/${encodeURIComponent(sub)}/revoke`);
export const adminReleaseSlot = (sub: string) =>
  call<{ user: AdminUser }>("POST", `/api/admin/users/${encodeURIComponent(sub)}/release-slot`);
export const adminSetIssuance = (enabled: boolean) =>
  call<{ issuanceEnabled: boolean }>("PUT", "/api/admin/issuance", { enabled });
export const getAdminCosts = (refresh = false) =>
  call<AdminCosts>("GET", `/api/admin/costs${refresh ? "?refresh=1" : ""}`);
export const adminSuspend = (sub: string) =>
  call<{ user: AdminUser }>("POST", `/api/admin/users/${encodeURIComponent(sub)}/suspend`);
export const adminResume = (sub: string) =>
  call<{ user: AdminUser }>("POST", `/api/admin/users/${encodeURIComponent(sub)}/resume`);
export const adminSetRegistrationCap = (cap: number) =>
  call<{ registrationCap: number; reservedSlots: number }>("PUT", "/api/admin/registration-cap", { cap });
export const adminSetCredentialAge = (days: number | null) =>
  call<{ credentialAgeDays: number | null }>("PUT", "/api/admin/credential-age", { days });
/** Register a participant (Cognito emails a temporary password). resend: new temporary password. */
export const adminInviteUser = (email: string, resend = false) =>
  call<{ email: string; resent: boolean }>("POST", "/api/admin/users", { email, resend });
