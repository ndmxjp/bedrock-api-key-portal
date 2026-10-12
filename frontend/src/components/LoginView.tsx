import { type FormEvent, useEffect, useState } from "react";
import {
  completePasswordReset,
  confirmRegistration,
  register,
  requestPasswordReset,
  resendRegistrationCode,
  setNewPassword,
  signInWithPassword,
} from "../auth";
import { authErrorMessage } from "../messages";
import { passwordProblems } from "../password";
import { Alert } from "./Alert";

type Mode = "signIn" | "newPassword" | "forgot" | "reset" | "register" | "confirm";

const RULES = [
  { key: "12文字以上", label: "12 文字以上" },
  { key: "英大文字", label: "英大文字" },
  { key: "英小文字", label: "英小文字" },
  { key: "数字", label: "数字" },
] as const;

const RESEND_WAIT_SECONDS = 60;

export function LoginView({
  onSignedIn,
  notice,
  allowedDomains = [],
}: {
  onSignedIn: () => void;
  notice?: string;
  allowedDomains?: string[];
}) {
  const [mode, setMode] = useState<Mode>("signIn");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [newPw, setNewPw] = useState("");
  const [newPw2, setNewPw2] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [wait, setWait] = useState(0);

  useEffect(() => {
    if (wait <= 0) return;
    const t = setTimeout(() => setWait((w) => w - 1), 1000);
    return () => clearTimeout(t);
  }, [wait]);

  const normalizedEmail = email.trim().toLowerCase();
  const problems = passwordProblems(newPw);
  const newPwValid = problems.length === 0 && newPw === newPw2;
  const domain = normalizedEmail.split("@")[1] ?? "";
  const domainOk = allowedDomains.length === 0 || allowedDomains.includes(domain);
  const domainLabel = allowedDomains.map((d) => `@${d}`).join(" / ");

  function go(next: Mode, message: string | null = null) {
    setMode(next);
    setError(null);
    setInfo(message);
    setPassword("");
    setNewPw("");
    setNewPw2("");
    setCode("");
  }

  async function run(action: () => Promise<void>) {
    setError(null);
    setBusy(true);
    try {
      await action();
    } catch (err) {
      setError(authErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const onSignIn = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      const result = await signInWithPassword(normalizedEmail, password);
      if (result === "done") return onSignedIn();
      if (result === "newPasswordRequired") return go("newPassword", "初回ログインです。新しいパスワードを設定してください。");
      if (result === "confirmRequired") {
        setWait(RESEND_WAIT_SECONDS);
        return go("confirm", "メールアドレスの確認が済んでいません。メールに届いた確認コードを入力してください。");
      }
      await requestPasswordReset(normalizedEmail);
      go("reset", "パスワードの再設定が必要です。メールに届いた確認コードと新しいパスワードを入力してください。");
    });
  };

  const onSetNewPassword = (e: FormEvent) => {
    e.preventDefault();
    if (!newPwValid) return;
    void run(async () => {
      await setNewPassword(newPw);
      onSignedIn();
    });
  };

  const onForgot = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      await requestPasswordReset(normalizedEmail);
      go("reset", `${normalizedEmail} が登録済みであれば、確認コードを送信しました。`);
    });
  };

  const onReset = (e: FormEvent) => {
    e.preventDefault();
    if (!newPwValid) return;
    void run(async () => {
      await completePasswordReset(normalizedEmail, code.trim(), newPw);
      go("signIn", "パスワードを再設定しました。新しいパスワードでログインしてください。");
    });
  };

  const onRegister = (e: FormEvent) => {
    e.preventDefault();
    if (!newPwValid || !domainOk) return;
    void run(async () => {
      await register(normalizedEmail, newPw);
      setWait(RESEND_WAIT_SECONDS);
      go("confirm", `${normalizedEmail} に確認コードを送信しました。`);
    });
  };

  const onConfirm = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      if (await confirmRegistration(normalizedEmail, code.trim())) return onSignedIn();
      go("signIn", "メールアドレスを確認しました。登録したパスワードでログインしてください。");
    });
  };

  const onResendConfirm = () => {
    if (wait > 0) return;
    void run(async () => {
      await resendRegistrationCode(normalizedEmail);
      setWait(RESEND_WAIT_SECONDS);
      setInfo("確認コードを再送しました。");
    });
  };

  const newPasswordFields = (
    <>
      <label className="field">
        <span>新しいパスワード</span>
        <input
          type="password"
          autoComplete="new-password"
          required
          value={newPw}
          onChange={(e) => setNewPw(e.target.value)}
          disabled={busy}
        />
      </label>
      <label className="field">
        <span>新しいパスワード（確認）</span>
        <input
          type="password"
          autoComplete="new-password"
          required
          value={newPw2}
          onChange={(e) => setNewPw2(e.target.value)}
          disabled={busy}
        />
      </label>
      <ul className="rules" aria-label="パスワードの要件">
        {RULES.map((r) => {
          const ok = newPw.length > 0 && !problems.includes(r.key);
          return (
            <li key={r.key} className={ok ? "ok" : ""}>
              <span className="rule-dot" aria-hidden="true">
                {ok ? "✓" : ""}
              </span>
              {r.key === "12文字以上" && newPw.length > 0 && newPw.length < 12
                ? `12 文字以上（あと ${12 - newPw.length} 文字）`
                : r.label}
            </li>
          );
        })}
      </ul>
      {newPw2 && newPw !== newPw2 && <p className="field-error">確認用のパスワードが一致しません。</p>}
    </>
  );

  const stepper = (active: 1 | 2) => (
    <ol className="stepper" aria-label="登録の手順">
      {["登録", "メール確認", "完了"].map((label, i) => {
        const n = (i + 1) as 1 | 2 | 3;
        return (
          <li key={label} className={n === active ? "current" : n < active ? "done" : ""} aria-current={n === active ? "step" : undefined}>
            <span className="step-n">{n}</span>
            {label}
          </li>
        );
      })}
    </ol>
  );

  return (
    <section className="card narrow" aria-labelledby="login-title">
      <h2 id="login-title" className={mode === "signIn" ? "tight" : undefined}>
        {
          {
            signIn: "ログイン",
            newPassword: "パスワードの設定",
            forgot: "パスワードの再設定",
            reset: "パスワードの再設定",
            register: "新規登録",
            confirm: "メールアドレスの確認",
          }[mode]
        }
      </h2>
      {mode === "signIn" && <p className="muted small subtitle">{domainLabel || "登録済み"} のアカウントでログイン</p>}
      {mode === "register" && stepper(1)}
      {mode === "confirm" && stepper(2)}
      {notice && <Alert tone="warning">{notice}</Alert>}
      {error && <Alert tone="error">{error}</Alert>}
      {info && !error && <Alert tone="info">{info}</Alert>}

      {mode === "signIn" && (
        <form onSubmit={onSignIn} className="stack">
          <label className="field">
            <span>メールアドレス</span>
            <input
              type="email"
              inputMode="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="name@example.com"
              disabled={busy}
            />
          </label>
          <label className="field">
            <span>パスワード</span>
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={busy}
            />
          </label>
          <button type="submit" className="btn primary" disabled={busy || !normalizedEmail || !password}>
            {busy ? "ログイン中…" : "ログイン"}
          </button>
          <div className="row between">
            <button type="button" className="btn link" onClick={() => go("register")} disabled={busy}>
              新規登録
            </button>
            <button type="button" className="btn link" onClick={() => go("forgot")} disabled={busy}>
              パスワードを忘れた場合
            </button>
          </div>
        </form>
      )}

      {mode === "register" && (
        <form onSubmit={onRegister} className="stack">
          {domainLabel && <Alert tone="info">登録できるのは {domainLabel} のメールアドレスだけです</Alert>}
          <label className="field">
            <span>メールアドレス</span>
            <input
              type="email"
              inputMode="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="name@example.com"
              disabled={busy}
            />
          </label>
          {normalizedEmail.includes("@") && domain && !domainOk && (
            <p className="field-error" role="alert">
              {domainLabel} 以外のメールアドレスは登録できません
            </p>
          )}
          {newPasswordFields}
          <button type="submit" className="btn primary" disabled={busy || !domainOk || !domain || !newPwValid}>
            {busy ? "登録中…" : "登録する"}
          </button>
          <button type="button" className="btn link" onClick={() => go("signIn")} disabled={busy}>
            ログインに戻る
          </button>
        </form>
      )}

      {mode === "confirm" && (
        <form onSubmit={onConfirm} className="stack">
          <p className="muted">
            次のアドレスに確認コードを送りました。
            <br />
            <span className="email-chip">
              {normalizedEmail}
              <button type="button" className="chip-edit" onClick={() => go("register")} aria-label="メールアドレスを変更" disabled={busy}>
                ✎
              </button>
            </span>
          </p>
          <label className="field">
            <span>確認コード</span>
            <input
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={8}
              required
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
              className="otp"
              disabled={busy}
            />
          </label>
          <button type="submit" className="btn primary" disabled={busy || code.length < 6}>
            {busy ? "確認中…" : "確認してログイン"}
          </button>
          <p className="center">
            <button type="button" className="btn link quiet" onClick={onResendConfirm} disabled={busy || wait > 0}>
              {wait > 0 ? (
                <>
                  コードを再送 <span className="mono">({wait} 秒)</span>
                </>
              ) : (
                "コードを再送"
              )}
            </button>
          </p>
        </form>
      )}

      {mode === "newPassword" && (
        <form onSubmit={onSetNewPassword} className="stack">
          <p className="email-chip">{normalizedEmail}</p>
          {newPasswordFields}
          <button type="submit" className="btn primary" disabled={busy || !newPwValid}>
            {busy ? "設定中…" : "パスワードを設定してログイン"}
          </button>
        </form>
      )}

      {mode === "forgot" && (
        <form onSubmit={onForgot} className="stack">
          <p className="muted">登録済みのメールアドレスに、再設定用の確認コードを送ります。</p>
          <label className="field">
            <span>メールアドレス</span>
            <input
              type="email"
              inputMode="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={busy}
            />
          </label>
          <button type="submit" className="btn primary" disabled={busy || !normalizedEmail}>
            {busy ? "送信中…" : "確認コードを送信"}
          </button>
          <button type="button" className="btn link" onClick={() => go("signIn")} disabled={busy}>
            ログインに戻る
          </button>
        </form>
      )}

      {mode === "reset" && (
        <form onSubmit={onReset} className="stack">
          <p className="email-chip">{normalizedEmail}</p>
          <label className="field">
            <span>確認コード</span>
            <input
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={8}
              required
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
              className="otp"
              disabled={busy}
            />
          </label>
          {newPasswordFields}
          <button type="submit" className="btn primary" disabled={busy || code.length < 6 || !newPwValid}>
            {busy ? "設定中…" : "パスワードを再設定"}
          </button>
          <button type="button" className="btn link" onClick={() => go("signIn")} disabled={busy}>
            ログインに戻る
          </button>
        </form>
      )}
    </section>
  );
}
