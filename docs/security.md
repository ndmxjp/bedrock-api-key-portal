# セキュリティ設計

## 信頼境界と検証

| 検証 | 場所 |
| --- | --- |
| JWT の署名・iss・aud・exp | API Gateway JWT authorizer |
| `token_use=id`, iss, aud, exp, `sub` 形式, `email_verified=true` | Lambda で再検証（authorizer の設定ミスでも安全側に倒れる） |
| ドメイン完全一致（`alice@evilcompany.co.jp` は不可、サブドメインも不可、全角などの非 ASCII は拒否） | Pre sign-up トリガー（早期拒否）と Lambda（必ず再検証） |
| 本人特定 | JWT の `sub` のみ。リクエスト本文は受け付けず、空以外は `400 INVALID_REQUEST` |
| IAM ユーザー名・期間・権限・モデル | すべてサーバー設定から決定。利用者は指定できない |

セルフ登録は許可ドメインのみです。Pre sign-up トリガーがドメインを完全一致で検査し（`evil-example.com` やサブドメインは不可）、確認コードでメール所有を証明します。API でも再検証します。メール属性は変更不可（`mutable: false`）で、登録時以外は書き換えられません。ログイン後にメールを書き換えて別ドメインに見せかけることはできません。

## 認証

- パスワード認証（SRP）。初期パスワードは管理者招待時に Cognito がメールで送り、初回ログインで変更を強制する（有効期限 7 日）
- パスワードの再設定は登録メールに届くコードで行う

## 管理者 API

- `/api/admin/*` は JWT 必須。さらに Lambda で `cognito:groups` に `PortalAdmins` が含まれ、かつ許可ドメイン・メール確認済みであることを確認する
- 管理者の付与は CLI（`AdminAddUserToGroup`）のみ。API・画面からは付与できない
- 入力を受け取るのはキルスイッチ（`{"enabled": true|false}` のみ）、登録枠（`{"cap": 整数}`、1〜1000）、有効期限（`{"days": 整数|null}`）、招待（`{"email": 文字列, "resend"?: 真偽値}`）、パス上の利用者 ID（形式を検証）だけ
- 招待（`POST /api/admin/users`）は Cognito の `AdminCreateUser` のみを呼ぶ。アドレスは正規化して許可ドメインと完全一致で検査し（Pre sign-up トリガーでも再検査）、初期パスワードは Cognito が生成して本人のメールにだけ送る。API Lambda の Cognito 権限はこの 1 アクションに限定し、パスワード設定・確認・グループ変更はできない。メールの日次送信上限を消費するため、ルート単位でスロットリング（1 req/s）する。ログにはアドレスを残さずドメインのみ記録する
- 一時停止中の利用者は、自分で失効・期限切れを経ても再発行できない（保留フラグを発行の条件式でも検査）
- 管理操作はアクターのサブジェクトハッシュ付きでログに残る。キー全文は管理 API でも返さない

## 秘密情報の取り扱い

- API キー全文は発行レスポンスでのみ返し、DynamoDB・Secrets Manager・ログには保存しない
- Lambda は事前に決めた安全なフィールドだけをログに出す。未処理例外は型名のみ記録する。`IssuedCredential` の `repr` は秘密値を伏せる
- API Gateway のアクセスログはヘッダー・クエリ文字列・claim を含まない形式
- レスポンスは `Cache-Control: no-store`。CloudFront の `/api/*` はキャッシュ無効
- SPA はキーを React の state にだけ保持する（Web Storage・URL・解析ツールには出さない）。閉じると破棄する。ページ離脱時に警告する
- Cognito トークンは sessionStorage（タブ単位）。ポータルは外部スクリプト・解析ツールを読み込まない（CSP `script-src 'self'`）
- 単体テストで、発行フロー全体の標準出力とストレージに秘密値が現れないことを確認している

## ネットワーク・ブラウザ

- API は CloudFront 経由の同一オリジン。HTTP API に CORS を設定していないため、他オリジンのブラウザからは呼べない
- セキュリティヘッダー: CSP（`default-src 'self'`、`connect-src` は自身と Cognito のみ、`frame-ancestors 'none'`）、HSTS、`X-Content-Type-Options`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`
- execute-api の既定エンドポイントは直接到達可能だが、全ルートで JWT が必須

## IP 制限（オプション）

- `portalAllowedCidrs`: CloudFront Function（viewer-request）で接続元 IPv4 を照合。`/api/*` には JSON、画面には HTML の 403 を返す。CloudFront は API Gateway へのリクエストに `X-Origin-Verify`（Secrets Manager の 48 文字の乱数）を付け、Lambda が定数時間比較で検証するため、execute-api の URL を直接使っても回避できない
- `keyAllowedCidrs`: 利用者ポリシーに `NotIpAddress aws:SourceIp` の Deny を追加。ポリシーは Permissions Boundary も兼ねるので、他の許可で上書きできない。Bedrock が代理で行う呼び出し（クロスリージョン推論のルーティング）は `aws:ViaAWSService` で除外

## 連打・不正試行対策

- HTTP API のスロットリング: 既定 10 rps / バースト 20、`POST /api/keys` は 1 rps / バースト 5
- ユーザー単位のクールダウン（既定 10 秒）を DynamoDB の条件式で強制
- 二重発行と人数上限は DynamoDB のトランザクションで原子的に判定し、並列リクエストでも超過しない
- Cognito のサインイン試行回数制限（連続失敗で一時ロック）
- WAF は未導入（Future Work）

## IAM

### 利用者ユーザー（`BedrockPoCUserPolicy`、Permissions Boundary にも同じものを設定）

- `bedrock:CallWithBearerToken`（API キーでの呼び出しに必須。リソース型が無いため `*`）
- 設定した推論プロファイルへの `bedrock:InvokeModel` / `InvokeModelWithResponseStream`（`aws:RequestedRegion` で Bedrock リージョンに限定）。Converse / ConverseStream もこの 2 つで認可される
- 基盤モデルは `bedrock:InferenceProfileArn` 条件で**許可したプロファイル経由のときだけ**呼び出せる。リージョン部分を `*` にしているのは、地域/グローバルのクロスリージョン推論のルーティング先をすべて列挙せずに済ませるため。プロファイル経由に限定しているので、他のモデルや直接呼び出しは許可されない
- `arn:aws:bedrock:<region>:<account>:project/default` への `bedrock:InvokeModel`（`InvokeDefaultProjectForResponsesApi`）。`bedrock-runtime` の OpenAI 互換 Responses API（Codex、OpenAI SDK）は推論対象に加えてアカウントの default project を認可するため。`bedrock:ModelArn` 条件で、上の文と同じ推論プロファイル／基盤モデルに限定する。Converse / InvokeModel / Chat Completions はこの文を使わない。保存済みレスポンスの取得・削除（`bedrock:GetInvoke` 等）と `bedrock-mantle` エンドポイントは許可しない
- コンソールパスワード・アクセスキーは発行しない

### キーの有効期限

既定は 7 日、上限 30 日。`maxCredentialAgeDays: "unlimited"` にすると、管理者は無期限のキーを発行できる設定にできる（仕様の「PoC の上限 30 日」からの運用判断による変更）。このとき発行 Lambda の `iam:ServiceSpecificCredentialAgeDays` 条件は外れるが、サービス名（bedrock）の条件は残る。無期限のキーは漏えいすると失効するまで使われ続けるため、失効・一時停止、`keyAllowedCidrs` による送信元 IP 制限、予算アラートを併用する。上限を下げて再デプロイすると、保存済みの長い設定は無視され初期値に戻る。

### `restrictRegion: false`

利用者ポリシーから `aws:RequestedRegion` の条件と推論プロファイル ARN のリージョン固定を外し、どのリージョンでも推論できるようにする（運用判断）。提供元・アクションの制限はそのまま。Marketplace の利用規約はリージョンごとに必要なため、自動同意は `bedrockRegion` でのみ行う。他リージョンの Marketplace モデルは、そのリージョンで同意が済むまで 403 になる。

### `allowAllModels` モード

仕様では「すべてのモデルを無制限に許可しない」としていたが、運用判断で全モデル許可を選べるようにした。その場合も、許可は推論アクションと `bedrockRegion` に限定される（モデル管理・Marketplace の権限は利用者に付与しない）。費用は Budgets と利用料表示で監視する。

### モデル利用規約の自動同意

デプロイ時だけ動く関数に `bedrock:CreateFoundationModelAgreement` と `aws-marketplace:Subscribe` / `ViewSubscriptions` を付与する（いずれもリソース単位の指定不可）。API Lambda と利用者には付与せず、API Lambda には明示的に Deny する。

### 発行 Lambda の実行ロール

| 許可 | 範囲 |
| --- | --- |
| `iam:CreateUser` | `user/bedrock-poc/poc-bedrock-*`、`iam:PermissionsBoundary` が本ポリシーの場合のみ |
| `iam:TagUser` | 同上、タグキーは `ManagedBy` / `PoC` / `PortalSubjectHash` のみ |
| `iam:AttachUserPolicy` | 同上、`iam:PolicyARN` が本ポリシー、かつ Boundary が本ポリシーの場合のみ |
| `iam:CreateServiceSpecificCredential` | 同上、`iam:ServiceSpecificCredentialServiceName = bedrock.amazonaws.com` かつ `iam:ServiceSpecificCredentialAgeDays <= 30` |
| `iam:List/Update/DeleteServiceSpecificCredential` | 同上 |
| `iam:GetUser` | 上記に加えて `user/poc-bedrock-*`（存在しないユーザーの照会はパス無しの ARN で評価されるため） |
| `ce:GetCostAndUsage` | `*`（Cost Explorer はリソース単位の権限がないため）。読み取りのみ。利用料表示に使用 |
| 明示的 Deny | アクセスキー・ログインプロファイル・インラインポリシー・ユーザー削除・Boundary 変更・ポリシーのデタッチ・グループ追加・MFA、`bedrock:*` |

ワイルドカードは「ユーザー名のサフィックス（`poc-bedrock-*`）」と「`bedrock:CallWithBearerToken` / `ce:GetCostAndUsage` / 明示的 Deny の `*`」だけです。ユーザー名はリクエストごとに決まるため、サフィックスのワイルドカードは避けられません。

必要に応じて、Organizations の SCP で `iam:ServiceSpecificCredentialAgeDays` の上限や `bedrock:CallWithBearerToken` の拒否をアカウント全体に適用してください。

## 残るリスク

- セルフ登録の入口が公開されている。許可ドメインのメールを受信できる人は誰でも登録できるため、人数上限（`registrationCap`）と利用ルールで抑える。組織のセキュリティ自動化がセルフサインアップを無効化する環境では、管理者招待に切り替わる
- 新規登録で既存アドレスを入力すると「登録済み」と表示されるため、登録有無は推測できる（Cognito のセルフ登録の仕様）

- ドメインのメールを受信できることは在籍の保証ではない。人数上限と利用ルールで緩和する
- 長期 API キーは漏えいすると有効期限まで使える。短い有効期限（既定 7 日）、失効機能、予算アラート、CloudTrail での監査で緩和する
- 管理者が `email_verified=true` で登録する。メール所有は、招待メールの初期パスワードを受け取れることで証明される
- パスワード認証のみで MFA は無効（PoC のため）。パスワードは 12 文字以上・英大小文字・数字必須、SRP によりパスワード自体は送信されない。本番化時は MFA または SSO を検討する
- Cognito 既定のメール送信は 1 日の送信数に上限があり、送信元アドレスも Cognito 既定になる。迷惑メールに分類される場合がある
