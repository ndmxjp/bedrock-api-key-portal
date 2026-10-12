export interface RuntimeConfig {
  region: string;
  userPoolId: string;
  userPoolClientId: string;
  /** UX hint for the sign-up form; enforced server-side. */
  allowedDomains?: string[];
}

/** Deploy-time config written by CDK to /config.json (no secrets). */
export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  const res = await fetch("/config.json", { cache: "no-store" });
  if (!res.ok) throw new Error("config.json を読み込めませんでした。");
  const c = (await res.json()) as Partial<RuntimeConfig>;
  if (!c.region || !c.userPoolId || !c.userPoolClientId) throw new Error("config.json の内容が不正です。");
  return c as RuntimeConfig;
}
