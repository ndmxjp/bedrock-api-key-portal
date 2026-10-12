# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A PoC web portal where people on an allowed email domain sign up, sign in, and issue one personal
long-term **Amazon Bedrock API key** (an IAM *service-specific credential* on a per-user IAM user).
Admins get a dashboard (kill switch, registration cap, key validity, suspend/revoke, per-user spend).
Three packages, each with its own toolchain; the root `Makefile` fans out to them:

| Dir | Stack | Tooling |
| --- | --- | --- |
| `backend/` | Python 3.14 Lambda (`portal` package) | `uv`, `pytest` + `moto` + botocore `Stubber`, `ruff` |
| `infra/` | AWS CDK v2 (TypeScript, ESM, run via `tsx`) | `npm`, `vitest` with `aws-cdk-lib/assertions` |
| `frontend/` | React 19 + Vite + Amplify Auth v6 (TypeScript) | `npm`, `vitest` + Testing Library (jsdom) |
| `scripts/` | Admin/ops CLIs (Python; run with the backend venv) | — |

User-facing text and docs are Japanese; code and comments are English.

## Commands

```bash
make install            # uv sync + npm ci in infra/ and frontend/
make lint               # ruff (src, tests, scripts) + tsc --noEmit in infra/ and frontend/
make test               # all three suites
make build              # frontend production build (required before synth/deploy)
make synth | diff | deploy | destroy      # CDK; CONFIG=portal.config.json STACK=BedrockApiKeyPortal
make deploy-ses         # only when ses.route53 is configured; deploy first, wait for verification
make requirements       # regenerate backend/requirements.txt after changing backend/pyproject.toml
```

Per package:

```bash
cd backend && uv run pytest tests/test_service.py -k double_issue      # single test
cd backend && uv run ruff check src tests ../scripts && uv run ruff format src tests ../scripts
cd infra && npx vitest run -t "issuer Lambda IAM"
cd frontend && npx vitest run src/AdminView.test.tsx
cd frontend && VITE_API_PROXY=https://<PortalUrl> npm run dev          # needs public/config.json from the deployed /config.json
cd backend && uv run python ../scripts/<script>.py --help              # scripts import portal.* and _common
```

Deploying needs `AWS_PROFILE` (and `CDK_DEFAULT_REGION`); the deploy config `infra/portal.config.json`
is gitignored (copy from `portal.config.example.json`). `cdk synth` fails on placeholder values on purpose.
Tests never touch AWS.

## Architecture (the parts that span files)

**Request path.** CloudFront serves the SPA from S3 and proxies `/api/*` to an HTTP API with a
Cognito JWT authorizer (ID token, aud = app client). Same origin, so there is no CORS. The CSP is
strict (`script-src 'self'`, `style-src 'self'`): no inline styles, no external fonts/CDNs — fonts
are bundled WOFF2 in `frontend/public/fonts`. `backend/src/portal/handler.py` routes on
`event["routeKey"]`; `claims.py` re-validates claims (token_use, iss, aud, exp, email_verified)
and the domain even though the authorizer already checked signature/expiry.

**Issuance state machine** (`service.py` + `repository.py`). DynamoDB is an index/cache; **IAM is
the source of truth** for key status (`_refresh` reconciles ACTIVE/SUSPENDED records with
`ListServiceSpecificCredentials` on every `/api/me`). Single table, `pk` only:
`USER#<sub>`, `META#COUNTER` (reservedSlots), `CONFIG#GLOBAL` (kill switch, cap, validity),
`CACHE#COSTS#<month>`, `CACHE#MODELS#v2`. `POST /api/keys` does one `TransactWriteItems` that
moves the user to `ISSUING` with a 90 s lease and (first time only) increments the slot counter
under the cap. `ISSUING_LEASE_SECONDS` (90) must stay above the API Lambda timeout (25 s) so a live
attempt is never taken over; `_classify_rejection` maps transaction cancellation to
409/429/403 codes. Admin writes to `CONFIG#GLOBAL` must use `update_item`, never `put_item`
(see `set_issuance_enabled` / `set_registration_cap` / `set_credential_age_days`).

**Suspension vs revocation.** `suspended` is a hold flag on the user record, separate from the
key status: it blocks issuance (checked in the begin-issuance condition expression) and survives
key expiry/self-revoke. `admin_resume` reactivates the same IAM credential when it still exists.

**IAM model.** Every user gets an IAM user under `/bedrock-poc/poc-bedrock-<sha256(sub)[:24]>`
with tags `ManagedBy=BedrockPoCPortal`, `PoC=true`, `PortalSubjectHash`. One customer-managed
policy (`infra/lib/bedrock-policy.ts`) is both the attached policy and the permissions boundary.
`issuer.py` refuses to touch a same-named IAM user whose path/tags/boundary don't match
(`IamUserConflict`). Cleanup/reconcile scripts only act on users matching path + prefix + tag.
The issuer Lambda's role is condition-pinned (boundary, policy ARN, `ServiceSpecificCredentialServiceName`,
age ≤ `maxCredentialAgeDays` unless unlimited) and explicitly denies inference and Marketplace actions.

**Config knobs that change the IAM policy** (`infra/lib/config.ts` → `bedrock-policy.ts`):
`allowAllModels` + `allowedProviders` (region-scoped wildcards; enumerating profiles would exceed the
6,144-char policy limit), `restrictRegion` (drops `aws:RequestedRegion`), `keyAllowedCidrs`
(`aws:SourceIp` deny with `aws:ViaAWSService=false` exemption), `maxCredentialAgeDays`
(`"unlimited"` → 0 everywhere; `0`/`UNLIMITED` means no expiry in the backend too).

**Deploy-time side effects.** A custom resource (`portal/agreements.py`, nonce = synth time) accepts
Marketplace model agreements for the target models in `bedrockRegion` on every deploy; agreements
are per region and are not undone on destroy. Fonts deploy separately with a 1-year immutable cache
and `contentType: font/woff2` (S3's guess would be blocked by `nosniff`).

**Costs.** `portal/costs.py` queries Cost Explorer grouped by the IAM-principal cost-allocation tag
`iamPrincipal/PortalSubjectHash` (activated in the *payer* account via `scripts/activate_cost_tag.py`;
data lags ≤24 h), cached 6 h in DynamoDB because each CE call is billed.

**Admin access** = Cognito group `PortalAdmins` in the ID token (`cognito:groups`, which API
Gateway renders as the string `"[a b]"` — see `_list_claim`), plus the same domain/verified checks.
Group membership is granted only via `scripts/portal_admin.py add-admin`.

**Optional IP restriction** (`portalAllowedCidrs`): a CloudFront Function (generated by
`infra/lib/ip-filter.ts`, IPv4 only, IPv6 disabled on the distribution) plus an `X-Origin-Verify`
secret that CloudFront adds and `handler._require_cloudfront` checks, so the execute-api URL can't
bypass it.

## Conventions worth knowing

- Secrets never persist: the API key appears only in the `POST /api/keys` response; logs are
  structured with allow-listed fields (`log.py`), `IssuedCredential.__repr__` redacts, tests assert
  the secret is absent from stdout/storage.
- API error bodies are `{"error": {"code", "message"}}` with Japanese messages from `errors.py`;
  add new codes there.
- `/api/me` response shape is asserted exactly in `backend/tests/test_service.py::test_me_not_issued`
  and mirrored by the `me()` fixture in `frontend/src/Dashboard.test.tsx` — update both when adding fields.
- Scripts default to dry-run and require `--apply --expected-account <id>` for writes.
- Frontend tests mock `./api` and `./auth` with `vi.hoisted`; the Amplify token storage is
  `sessionStorage`, so a browser test can inject tokens under `CognitoIdentityServiceProvider.<clientId>.*`.
- `docs/spec.md` (the original spec) is private and gitignored; `docs/implementation-plan.md`,
  `architecture.md`, `security.md` are the public design docs and are kept in sync with behavior changes.
