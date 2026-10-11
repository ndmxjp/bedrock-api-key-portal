# Bedrock API Key Self-Service Portal (PoC)

指定ドメインのメールアドレスを持つ PoC 参加者が、ブラウザからメール OTP で認証し、Amazon Bedrock の長期 API キー（IAM service-specific credential）を 1 人 1 つセルフサービス発行できる Web ポータル。

- 仕様書: [docs/spec.md](docs/spec.md)
- スタック: React + TypeScript + Vite / Cognito (email OTP) / API Gateway HTTP API + Lambda (Python) / DynamoDB / AWS CDK v2 (TypeScript)

## 構成（予定）

```text
.
├── README.md
├── docs/         # spec, architecture, security
├── infra/        # AWS CDK (TypeScript)
├── frontend/     # React / TypeScript / Vite
├── backend/      # Python Lambda
├── scripts/      # cleanup / 検証スクリプト
└── tests/        # Unit / Integration
```

実装は進行中。セットアップ・デプロイ・クリーンアップ手順は実装に合わせてここに追記する。
