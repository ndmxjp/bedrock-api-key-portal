import { useEffect, useState } from "react";
import type { IssuedKey } from "../api";
import { KEY_NOTICE, formatExpiry } from "../messages";
import { Alert } from "./Alert";
import { Icon } from "./Icon";

/**
 * One-time display of the API key. The secret lives only in React state: never in
 * Web Storage, URLs, logs or analytics. Dismissing drops it permanently.
 */
export function KeyReveal({ issued, onDone }: { issued: IssuedKey; onDone: () => void }) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  const [hidden, setHidden] = useState(false);

  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, []);

  async function copy() {
    setCopyError(false);
    try {
      await navigator.clipboard.writeText(issued.apiKey);
      setCopied(true);
    } catch {
      setCopyError(true);
    }
  }

  function done() {
    if (!copied && !window.confirm("キーをコピーしましたか？この画面を閉じると二度と表示できません。")) return;
    onDone();
  }

  return (
    <section className="card reveal" aria-labelledby="reveal-title">
      <div className="reveal-body">
        <div className="reveal-head">
          <span className="reveal-icon" aria-hidden="true">
            <Icon name="check" />
          </span>
          <div>
            <h2 id="reveal-title">API キーを発行しました</h2>
            <p className="muted small">発行直後は反映に数十秒かかることがあります</p>
          </div>
        </div>
        <Alert tone="error" title="このキーは今だけ表示されます">
          ページを閉じると二度と表示できません。安全な場所に保存してから閉じてください。
        </Alert>

        <div className="secret-box">
          <div className="secret-bar">
            <span>API キー</span>
            <span className="secret-actions">
              <button type="button" className="btn dark small" onClick={() => setHidden((h) => !h)}>
                {hidden ? "表示する" : "隠す"}
              </button>
              <button type="button" className="btn primary small" onClick={copy}>
                {copied ? "コピーしました" : "コピー"}
              </button>
            </span>
          </div>
          <code className={hidden ? "secret masked" : "secret"} aria-label="APIキー">
            {hidden ? "•".repeat(32) : issued.apiKey}
          </code>
        </div>
        {copied && (
          <p className="copied-note">
            <span className="copied-ok">✓ コピーしました</span>
            <span className="muted small">
              クリップボードにキーが残っています。貼り付け後は別の文字列をコピーして上書きすることを推奨します
            </span>
          </p>
        )}
        {copyError && <Alert tone="error">コピーできませんでした。キーを選択して手動でコピーしてください。</Alert>}

        <div className="facts sunken">
          <div>
            <span className="fact-label">リージョン</span>
            <span className="fact-value mono">{issued.region}</span>
          </div>
          <div>
            <span className="fact-label">有効期限</span>
            <span className="fact-value">{formatExpiry(issued.expiresAt, true)}</span>
          </div>
          <div>
            <span className="fact-label">キー ID</span>
            <span className="fact-value mono">{issued.credentialId}</span>
          </div>
        </div>

        <p className="muted small">{KEY_NOTICE}</p>
      </div>
      <div className="reveal-foot">
        <span className="muted small">ページを離れるとブラウザが警告します</span>
        <button type="button" className="btn secondary" onClick={done}>
          保存しました（キーを閉じる）
        </button>
      </div>
    </section>
  );
}
