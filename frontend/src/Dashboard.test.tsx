import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Me } from "./api";
import { Dashboard } from "./components/Dashboard";
import { authErrorMessage } from "./messages";

const SECRET = "ABSKexample-secret-value-1234567890";

const api = vi.hoisted(() => ({ getMe: vi.fn(), issueKey: vi.fn(), revokeKey: vi.fn() }));
vi.mock("./api", async (orig) => ({ ...(await orig<typeof import("./api")>()), ...api }));
vi.mock("./auth", () => ({ getIdToken: vi.fn(async () => "token") }));

const { ApiError } = await import("./api");

function me(overrides: Partial<Me> = {}): Me {
  return {
    email: "alice@company.co.jp",
    emailVerified: true,
    allowed: true,
    status: "NOT_ISSUED",
    key: null,
    region: "us-west-2",
    allowedModels: ["us.example.model-v1:0"],
    credentialAgeDays: 7,
    issuanceEnabled: true,
    isAdmin: false,
    usage: null,
    suspended: false,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

describe("Dashboard", () => {
  it("issues a key, shows it once, and never stores it", async () => {
    api.getMe.mockResolvedValueOnce(me());
    api.issueKey.mockResolvedValueOnce({
      status: "ACTIVE",
      apiKey: SECRET,
      expiresAt: "2026-10-18T00:00:00Z",
      issuedAt: "2026-10-11T00:00:00Z",
      region: "us-west-2",
      credentialId: "ACCA••••••••CDEF",
      allowedModels: ["us.example.model-v1:0"],
    });
    const user = userEvent.setup();
    render(<Dashboard onAuthExpired={() => undefined} />);

    await user.click(await screen.findByRole("button", { name: "APIキーを発行" }));
    expect(await screen.findByText(SECRET)).toBeTruthy();
    expect(screen.getByText("このキーは今だけ表示されます")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "コピー" }));
    expect(await navigator.clipboard.readText()).toBe(SECRET);
    expect(screen.getByRole("button", { name: "コピーしました" })).toBeTruthy();

    const storage = JSON.stringify({ ...window.localStorage }) + JSON.stringify({ ...window.sessionStorage });
    expect(storage).not.toContain(SECRET);
    expect(window.location.href).not.toContain(SECRET);

    api.getMe.mockResolvedValueOnce(
      me({
        status: "ACTIVE",
        key: { credentialId: "ACCA••••••••CDEF", status: "ACTIVE", issuedAt: null, expiresAt: null },
      }),
    );
    await user.click(screen.getByRole("button", { name: "保存しました（キーを閉じる）" }));
    await screen.findByText("ACCA••••••••CDEF");
    expect(screen.queryByText(SECRET)).toBeNull();
    expect(document.body.innerHTML).not.toContain(SECRET);
  });

  it("shows masked metadata and revoke for an active key, never an issue button", async () => {
    api.getMe.mockResolvedValueOnce(
      me({
        status: "ACTIVE",
        key: { credentialId: "ACCA••••••••CDEF", status: "ACTIVE", issuedAt: "2026-10-11T00:00:00Z", expiresAt: "2026-10-18T00:00:00Z" },
      }),
    );
    api.revokeKey.mockResolvedValueOnce(
      me({ status: "REVOKED", key: { credentialId: "ACCA••••••••CDEF", status: "REVOKED", issuedAt: null, expiresAt: null } }),
    );
    const user = userEvent.setup();
    render(<Dashboard onAuthExpired={() => undefined} />);

    expect(await screen.findByText("有効")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /APIキーを(再)?発行/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "キーを失効する" }));
    expect(await screen.findByText("失効済み")).toBeTruthy();
    expect(screen.getByRole("button", { name: "APIキーを再発行" })).toBeTruthy();
  });

  it("blocks non-allowed domains", async () => {
    api.getMe.mockResolvedValueOnce(me({ email: "mallory@evil.example", allowed: false }));
    render(<Dashboard onAuthExpired={() => undefined} />);
    expect(await screen.findByText("このメールドメインは利用対象外です")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "APIキーを発行" })).toBeNull();
  });

  it("shows the kill-switch state", async () => {
    api.getMe.mockResolvedValueOnce(me({ issuanceEnabled: false }));
    render(<Dashboard onAuthExpired={() => undefined} />);
    expect(await screen.findByText("現在、APIキーの発行を停止しています")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "APIキーを発行" })).toBeNull();
  });

  it("shows server error messages (e.g. cap reached)", async () => {
    api.getMe.mockResolvedValue(me());
    api.issueKey.mockRejectedValueOnce(new ApiError(403, "REGISTRATION_CAP_REACHED", "PoCの登録人数の上限に達しています。"));
    const user = userEvent.setup();
    render(<Dashboard onAuthExpired={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: "APIキーを発行" }));
    expect(await screen.findByText("PoCの登録人数の上限に達しています。")).toBeTruthy();
  });

  it("signs out on 401", async () => {
    api.getMe.mockRejectedValueOnce(new ApiError(401, "UNAUTHENTICATED", "x"));
    const onAuthExpired = vi.fn();
    render(<Dashboard onAuthExpired={onAuthExpired} />);
    await waitFor(() => expect(onAuthExpired).toHaveBeenCalled());
  });
});

describe("suspension", () => {
  it("explains the hold and offers no issue/revoke actions", async () => {
    api.getMe.mockResolvedValueOnce(
      me({
        status: "SUSPENDED",
        suspended: true,
        key: { credentialId: "ACCA••••••••CDEF", status: "SUSPENDED", issuedAt: null, expiresAt: null },
      }),
    );
    render(<Dashboard onAuthExpired={() => undefined} />);
    expect(await screen.findByText("APIキーの利用が一時停止されています")).toBeTruthy();
    expect(screen.getByText("一時停止中")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /APIキーを(再)?発行/ })).toBeNull();
    // The design keeps "revoke" available while suspended (admins can also revoke).
    expect(screen.getByRole("button", { name: "キーを失効する" })).toBeTruthy();
  });
  it("blocks reissue while held even after the key expired", async () => {
    api.getMe.mockResolvedValueOnce(me({ status: "EXPIRED", suspended: true }));
    render(<Dashboard onAuthExpired={() => undefined} />);
    await screen.findByText("APIキーの利用が一時停止されています");
    expect(screen.queryByRole("button", { name: /APIキーを(再)?発行/ })).toBeNull();
  });
});

describe("usage guide", () => {
  it("collapses a long all-models list and picks a sensible example model", async () => {
    api.getMe.mockResolvedValueOnce(
      me({ allModels: true, allowedModels: ["global.amazon.nova-pro-v1:0", "us.anthropic.claude-sonnet-5-5", "us.meta.llama"] }),
    );
    render(<Dashboard onAuthExpired={() => undefined} />);
    expect(await screen.findByText("us-west-2 のすべてのモデル")).toBeTruthy();
    expect(screen.getByText("3 件")).toBeTruthy();
    expect(screen.getByLabelText("動作確認コマンド例").textContent).toContain("us.anthropic.claude-sonnet-5-5");
  });
});

describe("usage guide providers", () => {
  it("names the allowed providers", async () => {
    api.getMe.mockResolvedValueOnce(
      me({ allModels: true, modelProviders: ["anthropic", "openai"], allowedModels: ["us.anthropic.claude-sonnet-5-5"] }),
    );
    render(<Dashboard onAuthExpired={() => undefined} />);
    expect(await screen.findByText("us-west-2 の Anthropic・OpenAI のすべてのモデル")).toBeTruthy();
    expect(screen.getByText(/Anthropic・OpenAI 以外のモデルや us-west-2 以外のリージョンへのリクエストは拒否されます/)).toBeTruthy();
  });
});

describe("no-expiry keys", () => {
  it("explains unlimited validity and shows 無期限 for the key", async () => {
    api.getMe.mockResolvedValueOnce(me({ credentialAgeDays: null }));
    render(<Dashboard onAuthExpired={() => undefined} />);
    expect(await screen.findByText(/有効期限はありません（失効するまで使えます）/)).toBeTruthy();
  });
  it("shows 無期限 for an active key without expiry", async () => {
    api.getMe.mockResolvedValueOnce(
      me({ status: "ACTIVE", key: { credentialId: "ACCA••••••••CDEF", status: "ACTIVE", issuedAt: null, expiresAt: null } }),
    );
    render(<Dashboard onAuthExpired={() => undefined} />);
    expect(await screen.findByText("無期限")).toBeTruthy();
  });
});

describe("unrestricted region", () => {
  it("does not claim other regions are rejected", async () => {
    api.getMe.mockResolvedValueOnce(me({ regionRestricted: false, allModels: true, modelProviders: ["anthropic"] }));
    render(<Dashboard onAuthExpired={() => undefined} />);
    expect(await screen.findByText(/他のリージョンでも利用できます/)).toBeTruthy();
    expect(screen.queryByText(/以外のリージョンへのリクエストは拒否されます/)).toBeNull();
  });
});

describe("usage", () => {
  it("shows the month-to-date cost", async () => {
    api.getMe.mockResolvedValueOnce(
      me({ usage: { month: "2026-10", amount: "1.2346", currency: "USD", fetchedAt: "2026-10-11T12:00:00Z", available: true } }),
    );
    render(<Dashboard onAuthExpired={() => undefined} />);
    expect(await screen.findByText("今月の利用料")).toBeTruthy();
    expect(screen.getByText("$1.23")).toBeTruthy();
  });
  it("says when costs are unavailable", async () => {
    api.getMe.mockResolvedValueOnce(
      me({ usage: { month: "2026-10", amount: null, currency: "USD", fetchedAt: null, available: false } }),
    );
    render(<Dashboard onAuthExpired={() => undefined} />);
    expect(await screen.findByText("現在、利用料を取得できません。")).toBeTruthy();
  });
});

describe("authErrorMessage", () => {
  it("does not reveal whether the account exists", () => {
    expect(authErrorMessage({ name: "NotAuthorizedException", message: "Incorrect username or password." })).toBe(
      "メールアドレスまたはパスワードが正しくありません。",
    );
    expect(authErrorMessage({ name: "UserNotFoundException" })).toBe("メールアドレスまたはパスワードが正しくありません。");
  });
  it("maps expired temporary passwords and lockouts", () => {
    expect(
      authErrorMessage({ name: "NotAuthorizedException", message: "Temporary password has expired and must be reset by an administrator." }),
    ).toMatch("再招待");
    expect(authErrorMessage({ name: "NotAuthorizedException", message: "Password attempts exceeded" })).toMatch("上限");
  });
  it("falls back safely", () => {
    expect(authErrorMessage(new Error("internal detail"))).not.toContain("internal detail");
  });
});
