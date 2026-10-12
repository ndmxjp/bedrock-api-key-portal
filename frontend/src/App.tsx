import { useCallback, useEffect, useState } from "react";
import { getIdToken, logout } from "./auth";
import { AdminView } from "./components/AdminView";
import { Dashboard } from "./components/Dashboard";
import { LoginView } from "./components/LoginView";

type View = { kind: "loading" } | { kind: "signedOut"; notice?: string } | { kind: "signedIn" };

export function App({ allowedDomains = [] }: { allowedDomains?: string[] }) {
  const [view, setView] = useState<View>({ kind: "loading" });
  const [isAdmin, setIsAdmin] = useState(false);
  const [page, setPage] = useState<"key" | "admin">("key");
  const [email, setEmail] = useState<string | null>(null);

  useEffect(() => {
    void getIdToken().then((t) => setView(t ? { kind: "signedIn" } : { kind: "signedOut" }));
  }, []);

  const onSignOut = useCallback(async () => {
    await logout();
    setIsAdmin(false);
    setEmail(null);
    setPage("key");
    setView({ kind: "signedOut" });
  }, []);

  const onAuthExpired = useCallback(async () => {
    await logout();
    setIsAdmin(false);
    setPage("key");
    setView({ kind: "signedOut", notice: "認証の有効期限が切れました。再度ログインしてください。" });
  }, []);

  const brand = (
    <div className="brand">
      <span className="brand-mark" aria-hidden="true" />
      <div>
        <h1>Bedrock API キー発行ポータル</h1>
        <p className="brand-sub">PoC 参加者向けセルフサービス</p>
      </div>
    </div>
  );
  const footnote = (
    <p className="footnote">
      このポータルはPoC用です。メールドメインの確認は在籍の保証ではありません。利用はPoCの利用ルールに従ってください。
    </p>
  );

  if (view.kind !== "signedIn") {
    return (
      <div className="shell">
        <header className="topbar">{brand}</header>
        <main className="auth-main">
          {view.kind === "loading" && <p className="muted center">読み込み中…</p>}
          {view.kind === "signedOut" && (
            <LoginView
              notice={view.notice}
              allowedDomains={allowedDomains}
              onSignedIn={() => setView({ kind: "signedIn" })}
            />
          )}
        </main>
        <footer className="footer">{footnote}</footer>
      </div>
    );
  }

  return (
    <div className="shell">
      <header className="topbar">
        {brand}
        <nav className="topnav" aria-label="メニュー">
          {isAdmin && (
            <div className="segmented" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={page === "key"}
                className={page === "key" ? "seg active" : "seg"}
                onClick={() => setPage("key")}
              >
                APIキー
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={page === "admin"}
                className={page === "admin" ? "seg active" : "seg"}
                onClick={() => setPage("admin")}
              >
                管理
              </button>
            </div>
          )}
          {email && <span className="whoami">{email}</span>}
          <button type="button" className="btn secondary small" onClick={() => void onSignOut()}>
            ログアウト
          </button>
        </nav>
      </header>
      <main className="content">
        {page === "key" && (
          <Dashboard
            onAuthExpired={() => void onAuthExpired()}
            onAdminChange={setIsAdmin}
            onIdentity={setEmail}
          />
        )}
        {page === "admin" && isAdmin && <AdminView onAuthExpired={() => void onAuthExpired()} />}
      </main>
      <footer className="footer">{footnote}</footer>
    </div>
  );
}
