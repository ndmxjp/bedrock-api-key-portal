/** Map Cognito / Amplify errors to Japanese messages without leaking internals. */
export function authErrorMessage(e: unknown): string {
  const name = (e as { name?: string } | null)?.name ?? "";
  const message = (e as { message?: string } | null)?.message ?? "";
  if (message.includes("DOMAIN_NOT_ALLOWED")) return "このメールドメインでは登録できません。";
  switch (name) {
    case "UsernameExistsException":
      return "このメールアドレスは登録済みです。ログインするか、パスワードを再設定してください。";
    case "UserLambdaValidationException":
      return "このメールアドレスでは登録できません。";
    case "NotAuthorizedException":
      if (/sign ?up is not permitted/i.test(message)) {
        return "現在、新規登録を受け付けていません。管理者に参加登録を依頼してください。";
      }
      if (/attempts exceeded/i.test(message)) {
        return "ログインの試行回数が上限に達しました。しばらく時間をおいてから再試行してください。";
      }
      if (/temporary password has expired/i.test(message)) {
        return "初期パスワードの有効期限が切れています。管理者に再招待を依頼してください。";
      }
      return "メールアドレスまたはパスワードが正しくありません。";
    case "UserNotFoundException":
      return "メールアドレスまたはパスワードが正しくありません。";
    case "InvalidPasswordException":
      return "パスワードが要件を満たしていません。12文字以上で、英大文字・英小文字・数字を含めてください。";
    case "CodeMismatchException":
      return "確認コードが正しくありません。メールに記載されたコードを確認してください。";
    case "ExpiredCodeException":
      return "確認コードの有効期限が切れています。もう一度やり直してください。";
    case "LimitExceededException":
    case "TooManyRequestsException":
    case "TooManyFailedAttemptsException":
      return "試行回数が上限に達しました。しばらく時間をおいてから再試行してください。";
    case "InvalidParameterException":
    case "EmptySignInUsername":
    case "EmptySignInPassword":
    case "EmptyResetPasswordUsername":
      return "入力内容を確認してください。";
    case "CodeDeliveryFailureException":
      return "確認コードのメールを送信できませんでした。時間をおいて再試行するか、管理者に連絡してください。";
    case "NetworkError":
      return "通信に失敗しました。ネットワーク接続を確認してください。";
    default:
      return "認証処理でエラーが発生しました。時間をおいて再試行してください。";
  }
}

export const KEY_NOTICE =
  "このキーはAmazon BedrockのAPIを利用するためのものです。対応するアプリ/APIクライアントで使用してください。" +
  "Claude Desktopの通常チャットに直接設定することはできません。" +
  "キーは第三者に共有せず、Gitリポジトリにも保存しないでください。";

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(d);
}

/** Expiry for display: a date, "無期限" for a key without expiry, or "—" when there is no key. */
export function formatExpiry(expiresAt: string | null | undefined, hasKey: boolean): string {
  if (expiresAt) return formatDateTime(expiresAt);
  return hasKey ? "無期限" : "—";
}

export function formatAge(days: number | null): string {
  return days === null ? "無期限" : `${days} 日間`;
}

export function formatMonth(month: string): string {
  const [y, m] = month.split("-");
  return y && m ? `${y} 年 ${Number(m)} 月` : month;
}

/** Whole days until expiry (0 when it is today), or null when there is no expiry. */
export function remainingDays(expiresAt: string | null | undefined, now = new Date()): number | null {
  if (!expiresAt) return null;
  const ms = new Date(expiresAt).getTime() - now.getTime();
  if (Number.isNaN(ms)) return null;
  return Math.max(0, Math.floor(ms / 86_400_000));
}

export function formatMoney(amount: string | null | undefined, currency = "USD"): string {
  if (amount === null || amount === undefined) return "—";
  const n = Number(amount);
  if (!Number.isFinite(n)) return "—";
  return new Intl.NumberFormat("ja-JP", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: n !== 0 && Math.abs(n) < 0.01 ? 4 : 2,
  }).format(n);
}

export const USAGE_NOTE =
  "AWS Cost Explorer の値です（割引・クレジット適用前、UTC の月初から）。反映まで最大24時間かかり、数時間ごとに更新されます。";

export const STATUS_LABEL: Record<string, string> = {
  NOT_ISSUED: "未発行",
  ISSUING: "発行処理中",
  ACTIVE: "有効",
  SUSPENDED: "一時停止中",
  REVOKED: "失効済み",
  EXPIRED: "期限切れ",
  FAILED: "発行失敗",
};
