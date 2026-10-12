import { useState } from "react";

const PREFERRED = /anthropic\.claude-sonnet/;
const PROVIDER_LABEL: Record<string, string> = { anthropic: "Anthropic", openai: "OpenAI" };

export function UsageGuide({
  region,
  models,
  allModels = false,
  providers = [],
  regionRestricted = true,
}: {
  region: string;
  models: string[];
  allModels?: boolean;
  providers?: string[];
  regionRestricted?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const scope = providers.length ? `${providers.map((p) => PROVIDER_LABEL[p] ?? p).join("・")} ` : "";
  const model = models.find((m) => PREFERRED.test(m)) ?? models[0] ?? "<inference-profile-id>";
  const list = (
    <ul className="model-list">
      {models.length === 0 ? <li>—</li> : models.map((m) => <li key={m} className="mono">{m}</li>)}
    </ul>
  );
  const curl = [
    `export AWS_BEARER_TOKEN_BEDROCK="<発行したAPIキー>"`,
    `curl -sS -X POST "https://bedrock-runtime.${region}.amazonaws.com/model/${encodeURIComponent(model)}/converse" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -H "Authorization: Bearer $AWS_BEARER_TOKEN_BEDROCK" \\`,
    `  -d '{"messages":[{"role":"user","content":[{"text":"こんにちは"}]}]}'`,
  ].join("\n");

  async function copyCurl() {
    try {
      await navigator.clipboard.writeText(curl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }

  return (
    <section className="card panel" aria-labelledby="usage-title">
      <div className="panel-head">
        <h2 id="usage-title">使い方</h2>
      </div>
      <div className="panel-body stack">
        <dl className="guide">
          <dt>リージョン</dt>
          <dd>
            <span className="mono">{region}</span>
            {!regionRestricted && <span className="muted small"> （他のリージョンでも利用できます）</span>}
          </dd>
          <dt>利用できるモデル</dt>
          <dd>
            {allModels ? (
              <details>
                <summary className="model-toggle">
                  <span className="plain-text">{`${region} の${scope ? ` ${scope.slice(0, -1)} の` : ""}すべてのモデル`}</span>{" "}
                  <span className="pill">{models.length} 件</span> 一覧を表示
                </summary>
                {list}
              </details>
            ) : (
              list
            )}
          </dd>
          <dt>環境変数</dt>
          <dd>
            キーを <code className="inline-code">AWS_BEARER_TOKEN_BEDROCK</code> に設定します
          </dd>
        </dl>
        <div className="codeblock">
          <div className="codeblock-bar">
            <span>動作確認</span>
            <button type="button" className="btn link" onClick={() => void copyCurl()}>
              {copied ? "コピーしました" : "コピー"}
            </button>
          </div>
          <pre aria-label="動作確認コマンド例">{curl}</pre>
        </div>
        <ul className="bullets">
          <li>
            {allModels
              ? regionRestricted
                ? `${scope ? `${scope}以外のモデルや ` : ""}${region} 以外のリージョンへのリクエストは拒否されます`
                : `${scope ? `${scope}以外のモデルへのリクエストは拒否されます` : "モデルの提供状況はリージョンごとに異なります"}`
              : regionRestricted
                ? "許可外のモデル・リージョンへのリクエストは拒否されます"
                : "許可外のモデルへのリクエストは拒否されます"}
          </li>
          {allModels && <li>モデルによっては、提供元の利用申請が済んでいないと利用できない場合があります</li>}
          <li>Claude Desktop の通常チャットでは使えません</li>
        </ul>
      </div>
    </section>
  );
}
