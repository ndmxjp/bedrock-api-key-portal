import { useCallback, useEffect, useMemo, useState } from "react";
import {
  type AdminCosts,
  type AdminOverview,
  type AdminUser,
  ApiError,
  adminReleaseSlot,
  adminSetCredentialAge,
  adminResume,
  adminRevoke,
  adminSetIssuance,
  adminSetRegistrationCap,
  adminSuspend,
  getAdminCosts,
  getAdminOverview,
  getAdminUsers,
} from "../api";
import { STATUS_LABEL, USAGE_NOTE, formatAge, formatDateTime, formatExpiry, formatMoney } from "../messages";
import { Alert } from "./Alert";

const STATUS_ROWS = [
  { label: "有効", keys: ["ACTIVE"], dot: "active" },
  { label: "一時停止中", keys: ["SUSPENDED"], dot: "suspended" },
  { label: "発行処理中", keys: ["ISSUING"], dot: "issuing" },
  { label: "失効・期限切れ", keys: ["REVOKED", "EXPIRED"], dot: "muted" },
  { label: "発行失敗", keys: ["FAILED"], dot: "failed" },
] as const;

export function AdminView({ onAuthExpired }: { onAuthExpired: () => void }) {
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [costs, setCosts] = useState<AdminCosts | null>(null);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [capDraft, setCapDraft] = useState<string | null>(null);
  const [ageDraft, setAgeDraft] = useState<{ days: string; unlimited: boolean } | null>(null);

  const handleError = useCallback(
    (e: unknown) => {
      const err = e instanceof ApiError ? e : new ApiError(0, "UNKNOWN", "予期しないエラーが発生しました。");
      if (err.status === 401) return onAuthExpired();
      setError(err.message);
    },
    [onAuthExpired],
  );

  const load = useCallback(async () => {
    setError(null);
    try {
      const [ov, us] = await Promise.all([getAdminOverview(), getAdminUsers()]);
      setOverview(ov);
      setUsers(us.users);
    } catch (e) {
      handleError(e);
    }
    // Costs are optional: a Cost Explorer problem must not hide the rest of the screen.
    getAdminCosts().then(setCosts, () => setCosts(null));
  }, [handleError]);

  async function saveCap() {
    if (!overview || capDraft === null) return;
    const cap = Number(capDraft);
    if (!Number.isInteger(cap) || cap < 1 || cap > overview.maxRegistrationCap) {
      setError(`登録枠は 1〜${overview.maxRegistrationCap} の整数で指定してください。`);
      return;
    }
    const warn =
      cap < overview.reservedSlots
        ? `\n現在の使用数（${overview.reservedSlots}）より小さいため、新しい利用者は発行できなくなります（既存の利用者はそのまま使えます）。`
        : "";
    if (!window.confirm(`登録枠を ${cap} に変更しますか？${warn}`)) return;
    setBusy("cap");
    setError(null);
    try {
      await adminSetRegistrationCap(cap);
      setInfo(`登録枠を ${cap} に変更しました。`);
      setCapDraft(null);
      await load();
    } catch (e) {
      handleError(e);
    } finally {
      setBusy(null);
    }
  }

  async function saveAge() {
    if (!overview || ageDraft === null) return;
    const max = overview.maxCredentialAgeDays;
    const days = ageDraft.unlimited ? null : Number(ageDraft.days);
    if (days !== null && (!Number.isInteger(days) || days < 1 || (max !== null && days > max))) {
      setError(`有効期限は 1〜${max ?? 36600} 日の整数で指定してください。`);
      return;
    }
    const label = formatAge(days);
    const extra = days === null ? "\n無期限のキーは漏えいすると失効するまで使われ続けます。" : "";
    if (!window.confirm(`これから発行するキーの有効期限を「${label}」にしますか？（発行済みのキーは変わりません）${extra}`)) return;
    setBusy("age");
    setError(null);
    try {
      await adminSetCredentialAge(days);
      setInfo(`キーの有効期限を「${label}」に変更しました。`);
      setAgeDraft(null);
      await load();
    } catch (e) {
      handleError(e);
    } finally {
      setBusy(null);
    }
  }

  async function refreshCosts() {
    setBusy("costs");
    try {
      setCosts(await getAdminCosts(true));
    } catch (e) {
      handleError(e);
    } finally {
      setBusy(null);
    }
  }

  const costBySub = useMemo(
    () => new Map((costs?.users ?? []).map((u) => [u.sub, u.amount] as const)),
    [costs],
  );

  useEffect(() => {
    void load();
  }, [load]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (users ?? []).filter((u) => !q || (u.email ?? "").toLowerCase().includes(q));
  }, [users, query]);

  async function act(id: string, confirmText: string, action: () => Promise<unknown>, done: string) {
    if (!window.confirm(confirmText)) return;
    setBusy(id);
    setError(null);
    setInfo(null);
    try {
      await action();
      setInfo(done);
      await load();
    } catch (e) {
      handleError(e);
    } finally {
      setBusy(null);
    }
  }

  if (!overview || !users) {
    return (
      <section className="card">
        {error ? (
          <>
            <Alert tone="error">{error}</Alert>
            <button type="button" className="btn secondary" onClick={() => void load()}>
              再読み込み
            </button>
          </>
        ) : (
          <p className="muted">読み込み中…</p>
        )}
      </section>
    );
  }

  const enabled = overview.issuanceEnabled;

  return (
    <>
      {error && <Alert tone="error">{error}</Alert>}
      {info && !error && <Alert tone="success">{info}</Alert>}

      <section className="stats" aria-label="概要">
        <div className="stat">
          <p className="stat-label">新規発行</p>
          <p className={`stat-value ${enabled ? "ok" : "stop"}`}>{enabled ? "受付中" : "停止中"}</p>
          <button
            type="button"
            className={enabled ? "btn danger small" : "btn primary small"}
            disabled={busy !== null}
            onClick={() =>
              void act(
                "issuance",
                enabled
                  ? "新規発行を停止しますか？（発行済みキーはそのまま使えます）"
                  : "新規発行を再開しますか？",
                () => adminSetIssuance(!enabled),
                enabled ? "新規発行を停止しました。" : "新規発行を再開しました。",
              )
            }
          >
            {enabled ? "発行を停止" : "発行を再開"}
          </button>
        </div>
        <div className="stat">
          <p className="stat-label">登録枠</p>
          <p className="stat-value">
            {overview.reservedSlots}
            <span className="stat-sub"> / {overview.registrationCap}</span>
          </p>
          <meter className="meter" min={0} max={overview.registrationCap} value={overview.reservedSlots} aria-label="登録枠の使用率" />
          {capDraft === null ? (
            <button
              type="button"
              className="btn ghost small"
              disabled={busy !== null}
              onClick={() => setCapDraft(String(overview.registrationCap))}
            >
              上限を変更
            </button>
          ) : (
            <div className="cap-edit">
              <input
                type="number"
                inputMode="numeric"
                min={1}
                max={overview.maxRegistrationCap}
                value={capDraft}
                onChange={(e) => setCapDraft(e.target.value)}
                aria-label="新しい登録枠"
              />
              <button type="button" className="btn primary small" disabled={busy !== null} onClick={() => void saveCap()}>
                {busy === "cap" ? "保存中…" : "保存"}
              </button>
              <button type="button" className="btn ghost small" disabled={busy !== null} onClick={() => setCapDraft(null)}>
                取消
              </button>
            </div>
          )}
        </div>
        <div className="stat">
          <p className="stat-label">キーの有効期限</p>
          <p className="stat-value">{formatAge(overview.credentialAgeDays)}</p>
          <p className="muted small">これから発行するキーに適用</p>
          {ageDraft === null ? (
            <button
              type="button"
              className="btn ghost small"
              disabled={busy !== null}
              onClick={() =>
                setAgeDraft({
                  days: String(overview.credentialAgeDays ?? 7),
                  unlimited: overview.credentialAgeDays === null,
                })
              }
            >
              有効期限を変更
            </button>
          ) : (
            <div className="cap-edit">
              <input
                type="number"
                inputMode="numeric"
                min={1}
                max={overview.maxCredentialAgeDays ?? 36600}
                value={ageDraft.days}
                disabled={ageDraft.unlimited}
                onChange={(e) => setAgeDraft({ ...ageDraft, days: e.target.value })}
                aria-label="有効日数"
              />
              <span className="muted small">日</span>
              {overview.maxCredentialAgeDays === null && (
                <label className="check">
                  <input
                    type="checkbox"
                    checked={ageDraft.unlimited}
                    onChange={(e) => setAgeDraft({ ...ageDraft, unlimited: e.target.checked })}
                  />
                  無期限
                </label>
              )}
              <button type="button" className="btn primary small" disabled={busy !== null} onClick={() => void saveAge()}>
                {busy === "age" ? "保存中…" : "保存"}
              </button>
              <button type="button" className="btn ghost small" disabled={busy !== null} onClick={() => setAgeDraft(null)}>
                取消
              </button>
            </div>
          )}
        </div>
        <div className="stat">
          <p className="stat-label">今月の利用料（{costs?.month ?? "—"}）</p>
          <p className="stat-value">
            {costs?.available ? formatMoney(costs.amount, costs.currency) : "—"}
          </p>
          <p className="muted small">
            {costs
              ? costs.available
                ? `最終取得: ${formatDateTime(costs.fetchedAt)}`
                : "Cost Explorer から取得できません"
              : "取得中…"}
          </p>
          <button type="button" className="btn ghost small" onClick={() => void refreshCosts()} disabled={busy !== null}>
            {busy === "costs" ? "取得中…" : "再取得"}
          </button>
        </div>
        <div className="stat">
          <p className="stat-label">キーの状態</p>
          <ul className="plain status-counts">
            {STATUS_ROWS.map((r) => (
              <li key={r.label}>
                <span className="status-name">
                  <span className={`dot dot-${r.dot}`} aria-hidden="true" />
                  {r.label}
                </span>
                <b>{r.keys.reduce((n, k) => n + (overview.statusCounts[k] ?? 0), 0)}</b>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section className="card panel" aria-labelledby="users-title">
        <div className="panel-head">
          <h2 id="users-title">
            利用者 <span className="count-muted">{users.length}</span>
          </h2>
          <span className="grow" />
          <label className="search">
            <span className="sr-only">メールアドレスで絞り込み</span>
            <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="メールアドレスで絞り込み" />
          </label>
          <button type="button" className="btn secondary small" onClick={() => void load()} disabled={busy !== null}>
            更新
          </button>
        </div>

        {filtered.length === 0 ? (
          <p className="muted panel-body">該当する利用者はいません。</p>
        ) : (
          <div className="table-scroll">
          <table className="users">
            <thead>
              <tr>
                <th>メールアドレス</th>
                <th>状態</th>
                <th>キー ID</th>
                <th>有効期限</th>
                <th>登録枠</th>
                <th className="num">今月の利用料</th>
                <th className="num">操作</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((u) => {
                const canRevoke = u.status === "ACTIVE" || u.status === "SUSPENDED";
                const canSuspend = u.status === "ACTIVE" && !u.suspended;
                const canResume = u.suspended;
                const canRelease =
                  u.slotReserved && !["ACTIVE", "ISSUING", "SUSPENDED"].includes(u.status);
                return (
                  <tr key={u.sub}>
                    <td data-label="メール" className="email-cell">
                      {u.email ? (
                        <>
                          {u.email.split("@")[0]}
                          <wbr />@{u.email.split("@").slice(1).join("@")}
                        </>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td data-label="状態">
                      <span className={`badge badge-${u.status.toLowerCase()}`}>{STATUS_LABEL[u.status] ?? u.status}</span>
                      {u.suspended && u.status !== "SUSPENDED" && <span className="badge badge-expired">停止中</span>}
                      {u.errorCode && <span className="muted small"> {u.errorCode}</span>}
                    </td>
                    <td data-label="キー" className="mono">
                      {u.credentialId ?? "—"}
                    </td>
                    <td data-label="有効期限">{formatExpiry(u.expiresAt, Boolean(u.credentialId))}</td>
                    <td data-label="枠">{u.slotReserved ? "使用中" : "—"}</td>
                    <td data-label="利用料" className="num">
                      {costs?.available ? formatMoney(costBySub.get(u.sub) ?? "0", costs.currency) : "—"}
                    </td>
                    <td className="actions">
                      {canSuspend && (
                        <button
                          type="button"
                          className="btn secondary small"
                          disabled={busy !== null}
                          onClick={() =>
                            void act(
                              u.sub,
                              `${u.email} のキーを一時停止しますか？再開するまで推論できず、新しいキーも発行できません。`,
                              () => adminSuspend(u.sub),
                              `${u.email} のキーを一時停止しました。`,
                            )
                          }
                        >
                          一時停止
                        </button>
                      )}
                      {canResume && (
                        <button
                          type="button"
                          className="btn primary small"
                          disabled={busy !== null}
                          onClick={() =>
                            void act(
                              u.sub,
                              `${u.email} の一時停止を解除しますか？有効期限内のキーは再び使えるようになります。`,
                              () => adminResume(u.sub),
                              `${u.email} の一時停止を解除しました。`,
                            )
                          }
                        >
                          再開
                        </button>
                      )}
                      {canRevoke && (
                        <button
                          type="button"
                          className="btn danger small"
                          disabled={busy !== null}
                          onClick={() =>
                            void act(
                              u.sub,
                              `${u.email} のキーを失効しますか？元に戻せません（一時停止なら再開できます）。`,
                              () => adminRevoke(u.sub),
                              `${u.email} のキーを失効しました。`,
                            )
                          }
                        >
                          {busy === u.sub ? "処理中…" : "失効"}
                        </button>
                      )}
                      {canRelease && (
                        <button
                          type="button"
                          className="btn secondary small"
                          disabled={busy !== null}
                          onClick={() =>
                            void act(
                              u.sub,
                              `${u.email} の登録枠を解放しますか？再発行時は新たに枠を使います。`,
                              () => adminReleaseSlot(u.sub),
                              `${u.email} の登録枠を解放しました。`,
                            )
                          }
                        >
                          {busy === u.sub ? "処理中…" : "枠を解放"}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </div>
        )}
        <p className="muted small panel-foot">
          状態は登録情報に基づく表示です。IAM 側の最新状態は、利用者がポータルを開いたときに反映されます。
          利用料は{USAGE_NOTE}
        </p>
      </section>
    </>
  );
}
