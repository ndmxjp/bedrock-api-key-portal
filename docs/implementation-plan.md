# 実装計画 — Bedrock API Key Self-Service Portal (PoC)

本書は実装順序・設計判断・検証方法をまとめたもの（元の仕様書は非公開）。

> 実装済み。決定事項: Cognito のパスワード認証（SRP、Lite、Cognito 既定のメール送信、SES 不使用）＋管理者招待制（セルフサインアップが無効化される環境向け）、Python 3.14 / Node 24 LTS / TypeScript 7.0 / CDK 2.273 / React 19.3 / Vite 8.3。テストは各パッケージ配下に配置。

## 0. 事前検証で確定した事実（AWS公式ドキュメント）

| 項目 | 結論 | 実装への影響 |
| --- | --- | --- |
| Cognito Email OTP | **Essentials 以上**の feature plan と **SES メール設定 (`EmailSendingAccount=DEVELOPER`) が必須**。`COGNITO_DEFAULT` では不可 | CDK で `FeaturePlan.ESSENTIALS` + SES identity を必須パラメータ化 |
| SES サンドボックス | サンドボックスでは検証済みアドレスにしか送信できない | README に「本番アクセス申請」or「参加者10名のアドレスを個別検証」を手順化 |
| パスワードレスの UI | Managed login なら対応、classic Hosted UI は非対応。SDK から `USER_AUTH` + `EMAIL_OTP` の直接呼び出しも可 | SPA から Amplify Auth v6 で `USER_AUTH` / `preferredChallenge: EMAIL_OTP` を直接呼ぶ（リダイレクト不要、自前の日本語UI） |
| CreateServiceSpecificCredential | `ServiceName=bedrock.amazonaws.com`, `CredentialAgeDays`（Bedrock/CloudWatch Logs のみ有効） | そのまま採用 |
| 秘密値のレスポンスキー | Bedrock ガイドは `ServiceApiKeyValue` と記載しているが、**最新 botocore (1.43) の IAM モデルには存在せず、`ServiceCredentialSecret` が正**（`ServiceCredentialAlias` も追加） | `ServiceSpecificCredential.ServiceCredentialSecret` を使用。botocore Stubber のテストで固定。Bedrock ガイドの Python サンプルは擬似コード（`create_iam_user` 等は実在しない） |
| キー利用時の IAM アクション | APIキー呼び出しは `bedrock:CallWithBearerToken` を要求 | ユーザーポリシーに明示的に含める |
| 発行制御の条件キー | `iam:ServiceSpecificCredentialServiceName`, `iam:ServiceSpecificCredentialAgeDays` | 発行 Lambda ロールで「bedrock限定・30日以下」を強制 |

## 1. 主要な設計判断

1. **同一オリジン構成**: CloudFront に `/*`→S3(OAC), `/api/*`→HTTP API の2ビヘイビア。CORS が実質不要になり、APIの公開オリジンも1つに絞れる。HTTP API 側の CORS も CloudFront ドメインのみ許可しておく。
2. **トークン**: API には **ID トークン**を送る（`email` / `email_verified` を含むため）。HTTP API JWT authorizer が署名・iss・aud(client id)・exp を検証し、Lambda で `token_use=id`、`email_verified=="true"`、ドメイン完全一致を再検証。
3. **初回ユーザー**: `signUp`（パスワードなし）→ `confirmSignUp`（メールコード = メール検証）→ `autoSignIn`。2回目以降は `signIn(USER_AUTH, EMAIL_OTP)`。Pre sign-up トリガーで許可ドメイン外を早期拒否（Lambda 側でも再検証）。
4. **トークン保存**: Amplify の token storage を sessionStorage（または in-memory）に変更。API キーは React state のみで保持し、Storage/ログ/解析には一切出さない。
5. **Lambda は1関数**（`/api/me`, `/api/keys`, `/api/keys/revoke` をルーティング）。Python 3.12、**boto3 を同梱**（ランタイム同梱版が古く新パラメータ未対応のリスク回避）。
6. **Bedrock 権限**: CDK で顧客管理ポリシー `BedrockPoCUserPolicy` を作成し `AttachUserPolicy`。同じポリシーを **Permissions Boundary** にも設定。中央で権限変更できる。
   - Allow: `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`（Converse/ConverseStream もこれで認可）, `bedrock:CallWithBearerToken`
   - Resource: 設定ファイルで列挙した推論プロファイル ARN と、そのルーティング先の foundation-model ARN のみ
   - Condition: `aws:RequestedRegion` を設定リージョンに限定（クロスリージョン推論のルーティング先は別途考慮）
   - モデル ID / 推論プロファイル ARN は**推測で書かない**。`config` はプレースホルダとし、`aws bedrock list-inference-profiles` で取得する手順を README に記載
7. **人数上限の定義**: 「発行枠＝ユーザー単位」。初回発行時に枠を1つ確保し、失効→再発行では枠を消費しない。管理スクリプトで枠解放可能。
8. **キルスイッチ**: DynamoDB の `CONFIG#GLOBAL.issuanceEnabled` を Lambda が毎回参照。`scripts/kill-switch` で即時切替（再デプロイ不要）。
9. **真実の情報源は IAM**: `GET /api/me` で状態が ACTIVE の場合、`ListServiceSpecificCredentials` で Status/ExpirationDate を確認し、DynamoDB を `EXPIRED`/`REVOKED` に補正。

## 2. DynamoDB 設計（単一テーブル）

| pk | 主な属性 |
| --- | --- |
| `USER#<sub>` | email, iamUserName, credentialId, status, issuedAt, expiresAt, updatedAt, operationId, leaseUntil, lastAttemptAt, errorCode, slotReserved |
| `META#COUNTER` | reservedSlots |
| `CONFIG#GLOBAL` | issuanceEnabled |

### 発行フロー（`POST /api/keys`）

1. JWT claim 検証 → ドメイン検証 → キルスイッチ確認
2. **TransactWriteItems**
   - USER: 条件 `attribute_not_exists(pk) OR status IN (REVOKED, EXPIRED, FAILED) OR (status=ISSUING AND leaseUntil < now)`、かつ `lastAttemptAt` によるクールダウン → `ISSUING`, 新 operationId, leaseUntil=now+60s
   - COUNTER（未確保ユーザーのみ）: 条件 `reservedSlots < cap` → `ADD reservedSlots 1`
   - 失敗理由を `CancellationReasons` から判別 → `409 ALREADY_ISSUED` / `409 ISSUING` / `403 REGISTRATION_CAP_REACHED` / `429`
3. IAM: `GetUser`（無ければ `CreateUser` Path=`/bedrock-poc/`, Tags, PermissionsBoundary）→ ポリシーアタッチ（冪等）
4. 既存の Bedrock 資格情報を列挙。**このユーザーに限り**、Inactive/期限切れ/ISSUING 中断で秘密未配布のものを Delete（上限2個対策）
5. `CreateServiceSpecificCredential` → USER を operationId 条件付きで `ACTIVE` に更新 → キーを返却
6. 5 の DynamoDB 更新が失敗した場合は作成した資格情報を即 Delete し `FAILED`（ロールバック）

`POST /api/keys/revoke`: `UpdateServiceSpecificCredential(Inactive)` → `REVOKED`。再実行しても同結果（冪等）。

## 3. 発行 Lambda 実行ロール（最小権限）

- `iam:CreateUser`: Resource `arn:aws:iam::<acct>:user/bedrock-poc/*`、Condition `iam:PermissionsBoundary` = BedrockPoCUserPolicy
- `iam:AttachUserPolicy`: 同 Resource、Condition `iam:PolicyARN` = BedrockPoCUserPolicy
- `iam:GetUser`, `iam:TagUser`, `iam:ListServiceSpecificCredentials`, `iam:UpdateServiceSpecificCredential`, `iam:DeleteServiceSpecificCredential`: 同 Resource
- `iam:CreateServiceSpecificCredential`: 同 Resource、Condition `ServiceName=bedrock.amazonaws.com` かつ `AgeDays <= 30`
- 明示 Deny: `iam:CreateAccessKey`, `iam:CreateLoginProfile`, `iam:DeleteUser`, `iam:PutUserPolicy` 等
- Bedrock 推論権限は付与しない。DynamoDB は当該テーブルのみ

## 4. 実装フェーズ

| # | フェーズ | 成果物 | 完了条件 |
| --- | --- | --- | --- |
| 1 | 雛形 | `infra/`(CDK TS), `backend/`(Python, pyproject, ruff), `frontend/`(Vite React TS), ルート Makefile | 各ディレクトリで lint/test が空で通る |
| 2 | Backend コア | `domain.py`(ドメイン正規化・完全一致), `claims.py`, `naming.py`(SHA-256→`poc-bedrock-<24hex>`), `repository.py`(DynamoDB 状態遷移), `issuer.py`(IAM 操作・ロールバック), `handler.py`(ルーティング・エラー形式・ログマスク) | 単体テスト緑 |
| 3 | Backend テスト | DynamoDB は moto、IAM は botocore Stubber。並列発行（スレッド）で有効キー1つ・上限超過なしを検証 | §10 のロジック系項目を網羅 |
| 4 | Infra | Cognito(Essentials, USER_AUTH, EMAIL_OTP, SES, Pre sign-up), HTTP API + JWT authorizer + ルート別スロットリング, Lambda, DynamoDB(PITR), S3+CloudFront(OAC, CSP ヘッダ), 顧客管理ポリシー, アクセスログ(ヘッダ非記録), CloudWatch アラーム+SNS, Budgets | `cdk synth` 成功、CDK assertions で IAM 条件キーを検証 |
| 5 | Frontend | ログイン（メール→コード→再送待機）、発行前、発行完了（一度きり表示・コピー）、発行済み（マスク・失効）、エラー各種。`config.json` をデプロイ時生成 | `npm run build`、vitest で状態遷移 |
| 6 | Scripts | `cleanup.py`（既定 dry-run、Path かつ `ManagedBy` タグ一致のユーザーのみ処理）、`reconcile.py`（孤児ユーザー/キー検出）、`kill-switch`、`verify-bedrock.sh`（許可/禁止モデル・失効後の推論確認） | dry-run 出力をレビュー |
| 7 | Docs | README（セットアップ、設定値、deploy、モデル権限、E2E、費用箇所、クリーンアップ）、`docs/architecture.md`、`docs/security.md`（ワイルドカード理由含む） | README どおりに手順が追える |

## 5. 設定値（`infra/config.ts` または cdk context）

`allowedDomains`, `credentialAgeDays`(7, ≤30 を synth 時に検証), `registrationCap`(10), `bedrockRegion`(us-west-2), `allowedInferenceProfileArns`, `allowedFoundationModelArns`, `sesIdentityArn`, `fromEmail`, `alarmEmail`, `monthlyBudgetUsd`, `appRegion`

## 6. 実装前に決めてほしいこと（未定でも仮値で進める）

| 項目 | 仮値 |
| --- | --- |
| アプリのデプロイ先リージョン | `ap-northeast-1` |
| SES の送信元ドメイン/アドレスと本番アクセス有無 | 未定。README に両パターンを記載 |
| 許可モデル（推論プロファイル） | プレースホルダ。デプロイ前に実アカウントで確認 |
| 許可ドメイン | `company.co.jp` |
| 予算アラートの金額と通知先 | パラメータ必須化 |

## 7. 対象外 / Future Work

`/api/keys/rotate`（MVP では失効→再発行で代替）、WAF（コストのためオプションフラグ）、Cognito Plus の脅威保護、IIC/SSO、短期認証情報への移行、管理画面。

## 8. リスク

- IAM の結果整合性で発行直後に推論が失敗し得る → UI で「数十秒後に再試行」を案内
- 一度きり表示のレスポンスが届かなかった場合は失効→再発行で回復（枠は消費しない）
- Cognito Essentials と SES は従量課金。料金は README でハードコードせず課金箇所のみ列挙
