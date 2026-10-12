import { useCallback, useEffect, useState } from "react";
import { ApiError, type IssuedKey, type Me, getMe, issueKey, revokeKey } from "../api";
import {
  KEY_NOTICE,
  STATUS_LABEL,
  USAGE_NOTE,
  formatDateTime,
  formatExpiry,
  formatMoney,
  formatMonth,
  remainingDays,
} from "../messages";
import { Alert } from "./Alert";
import { KeyReveal } from "./KeyReveal";
import { UsageGuide } from "./UsageGuide";

export function Dashboard({
  onAuthExpired,
  onAdminChange,
  onIdentity,
}: {
  onAuthExpired: () => void;
  onAdminChange?: (isAdmin: boolean) => void;
  onIdentity?: (email: string) => void;
}) {
  const [me, setMe] = useState<Me | null>(null);
  const [issued, setIssued] = useState<IssuedKey | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const handleError = useCallback(
    (e: unknown) => {
      const err = e instanceof ApiError ? e : new ApiError(0, "UNKNOWN", "予期しないエラーが発生しました。");
      if (err.status === 401) return onAuthExpired();
      setError(err);
    },
    [onAuthExpired],
  );

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const m = await getMe();
      setMe(m);
      onAdminChange?.(m.isAdmin);
      onIdentity?.(m.email);
    } catch (e) {
      handleError(e);
    }
  }, [handleError, onAdminChange, onIdentity]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function onIssue() {
    setBusy(true);
    setError(null);
    try {
      setIssued(await issueKey());
    } catch (e) {
      handleError(e);
      void getMe().then(setMe, () => undefined);
    } finally {
      setBusy(false);
    }
  }

  async function onRevoke() {
    if (!window.confirm("APIキーを失効させますか？失効したキーは元に戻せません。")) return;
    setBusy(true);
    setError(null);
    try {
      setMe(await revokeKey());
    } catch (e) {
      handleError(e);
    } finally {
      setBusy(false);
    }
  }

  function onRevealDone() {
    setIssued(null);
    void refresh();
  }

  if (issued) return <KeyReveal issued={issued} onDone={onRevealDone} />;

  if (!me) {
    return (
      <section className="card">
        {error ? (
          <>
            <Alert tone="error">{error.message}</Alert>
            <button type="button" className="btn secondary" onClick={() => void refresh()}>
              再読み込み
            </button>
          </>
        ) : (
          <p className="muted">読み込み中…</p>
        )}
      </section>
    );
  }

  const canIssue =
    me.allowed &&
    me.issuanceEnabled &&
    !me.suspended &&
    ["NOT_ISSUED", "REVOKED", "EXPIRED", "FAILED"].includes(me.status);
  const isReissue = me.status !== "NOT_ISSUED";

  return (
    <>
      <div className="two-col">
      <div className="main-col">
      {error && <Alert tone="error">{error.message}</Alert>}
      {!me.allowed && (
        <Alert tone="error" title="このメールドメインは利用対象外です">
          PoCの対象ドメインのメールアドレスでログインしてください。
        </Alert>
      )}
      {me.suspended && (
        <Alert tone="warning" title="APIキーの利用が一時停止されています">
          管理者により一時停止されています。停止中はキーで推論できず、新しいキーも発行できません。詳しくは管理者に問い合わせてください。
        </Alert>
      )}
      {me.allowed && !me.issuanceEnabled && (
        <Alert tone="warning" title="現在、APIキーの発行を停止しています">
          既に発行済みのキーの確認・失効は引き続き行えます。
        </Alert>
      )}

      <section className="card panel" aria-labelledby="status-title">
        <div className="panel-head">
          <h2 id="status-title">あなたの API キー</h2>
          <span className={`badge badge-${me.status.toLowerCase()}`}>{STATUS_LABEL[me.status] ?? me.status}</span>
          {me.status === "ISSUING" && <span className="muted small grow">反映に数十秒かかることがあります</span>}
          <span className="grow" />
          {(me.status === "ACTIVE" || me.status === "SUSPENDED") && (
            <button type="button" className="btn danger small" onClick={onRevoke} disabled={busy}>
              {busy ? "処理中…" : "キーを失効する"}
            </button>
          )}
          {me.status === "ISSUING" && (
            <button type="button" className="btn secondary small" onClick={() => void refresh()} disabled={busy}>
              更新
            </button>
          )}
        </div>

        {me.key && (
          <div className="facts">
            <div>
              <span className="fact-label">キー ID</span>
              <span className="fact-value mono">{me.key.credentialId}</span>
            </div>
            <div>
              <span className="fact-label">発行日時</span>
              <span className="fact-value">{formatDateTime(me.key.issuedAt)}</span>
            </div>
            <div>
              <span className="fact-label">有効期限</span>
              <span className="fact-value">
                {formatExpiry(me.key.expiresAt, true)}
                {me.status === "ACTIVE" && remainingDays(me.key.expiresAt) !== null && (
                  <span className="remaining">残り {remainingDays(me.key.expiresAt)} 日</span>
                )}
              </span>
            </div>
          </div>
        )}

        {me.status === "ACTIVE" && (
          <p className="muted small panel-body">
            キー全文はセキュリティのため再表示できません。紛失した場合は失効してから再発行してください。
          </p>
        )}

        {me.status === "FAILED" && !me.key && (
          <p className="muted small panel-body">前回の発行処理が完了しませんでした。もう一度発行してください。</p>
        )}

        {canIssue && (
          <div className="issue-panel panel-body">
            <p className="muted small">発行前に確認してください</p>
            <ul className="checklist">
              <li>
                {me.credentialAgeDays === null
                  ? "有効期限はありません（失効するまで使えます）。不要になったら必ず失効してください。"
                  : <>有効期限は発行から <b>{me.credentialAgeDays} 日間</b></>}
              </li>
              <li>
                キー全文は発行時に<b>一度だけ</b>表示されます
              </li>
              <li>利用は PoC 目的に限ります</li>
            </ul>
            <button type="button" className="btn primary block" onClick={onIssue} disabled={busy}>
              {busy ? "発行中…" : isReissue ? "APIキーを再発行" : "APIキーを発行"}
            </button>
          </div>
        )}
      </section>

      <UsageGuide
        region={me.region}
        models={me.allowedModels}
        allModels={me.allModels ?? false}
        providers={me.modelProviders ?? []}
        regionRestricted={me.regionRestricted ?? true}
      />
      </div>

      <aside className="side">
        {me.usage && (
          <section className="card" aria-labelledby="cost-title">
            <div className="row between baseline">
              <h2 id="cost-title">今月の利用料</h2>
              <span className="muted small">{formatMonth(me.usage.month)}</span>
            </div>
            {me.usage.available ? (
              <p className="money">{formatMoney(me.usage.amount, me.usage.currency)}</p>
            ) : (
              <p className="muted">現在、利用料を取得できません。</p>
            )}
            {me.usage.fetchedAt && <p className="muted small">最終取得 {formatDateTime(me.usage.fetchedAt)}</p>}
            <p className="muted small divider-top">{USAGE_NOTE}</p>
          </section>
        )}
        <section className="card" aria-labelledby="notice-title">
          <h2 id="notice-title">取り扱いの注意</h2>
          <p className="muted small">{KEY_NOTICE}</p>
        </section>
      </aside>
      </div>
    </>
  );
}
