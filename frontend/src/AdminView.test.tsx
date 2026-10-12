import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AdminOverview, AdminUser } from "./api";
import { AdminView } from "./components/AdminView";

const api = vi.hoisted(() => ({
  getAdminOverview: vi.fn(),
  getAdminUsers: vi.fn(),
  adminRevoke: vi.fn(),
  adminReleaseSlot: vi.fn(),
  adminSetIssuance: vi.fn(),
  getAdminCosts: vi.fn(),
  adminSuspend: vi.fn(),
  adminResume: vi.fn(),
  adminSetRegistrationCap: vi.fn(),
  adminSetCredentialAge: vi.fn(),
  adminInviteUser: vi.fn(),
}));
vi.mock("./api", async (orig) => ({ ...(await orig<typeof import("./api")>()), ...api }));
vi.mock("./auth", () => ({ getIdToken: vi.fn(async () => "token") }));
const { ApiError } = await import("./api");

const overview = (o: Partial<AdminOverview> = {}): AdminOverview => ({
  issuanceEnabled: true,
  reservedSlots: 2,
  registrationCap: 10,
  maxRegistrationCap: 1000,
  maxCredentialAgeDays: null,
  credentialAgeDays: 7,
  region: "us-west-2",
  allowedModels: [],
  allowedDomains: ["example.com"],
  statusCounts: { ACTIVE: 1, REVOKED: 1 },
  ...o,
});
const user = (o: Partial<AdminUser>): AdminUser => ({
  sub: "s",
  email: "x@example.com",
  status: "ACTIVE",
  credentialId: "ACCA••••••••0001",
  issuedAt: null,
  expiresAt: "2026-10-18T00:00:00Z",
  updatedAt: null,
  slotReserved: true,
  suspended: false,
  errorCode: null,
  ...o,
});

const ALICE = user({ sub: "a", email: "alice@example.com", status: "ACTIVE" });
const BOB = user({ sub: "b", email: "bob@example.com", status: "REVOKED" });

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  api.getAdminOverview.mockResolvedValue(overview());
  api.getAdminUsers.mockResolvedValue({ users: [ALICE, BOB] });
  api.getAdminCosts.mockResolvedValue({
    month: "2026-10",
    amount: "12.5",
    currency: "USD",
    fetchedAt: "2026-10-11T12:00:00Z",
    available: true,
    users: [
      { sub: "a", email: "alice@example.com", amount: "12.0" },
      { sub: "b", email: "bob@example.com", amount: "0.004" },
    ],
    unmatchedAmount: "0.5",
  });
});

function row(email: string) {
  return screen.getByText(email).closest("tr") as HTMLElement;
}

describe("AdminView", () => {
  it("shows overview and users with the right actions per state", async () => {
    render(<AdminView onAuthExpired={() => undefined} />);
    expect(await screen.findByText("受付中")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "利用者 2" })).toBeTruthy();
    expect(within(row("alice@example.com")).getByRole("button", { name: "失効" })).toBeTruthy();
    expect(within(row("alice@example.com")).queryByRole("button", { name: "枠を解放" })).toBeNull();
    expect(within(row("bob@example.com")).queryByRole("button", { name: "失効" })).toBeNull();
    expect(within(row("bob@example.com")).getByRole("button", { name: "枠を解放" })).toBeTruthy();
  });

  it("shows total and per-user costs, and can force a refresh", async () => {
    const u = userEvent.setup();
    render(<AdminView onAuthExpired={() => undefined} />);
    expect(await screen.findByText("$12.50")).toBeTruthy();
    expect(within(row("alice@example.com")).getByText("$12.00")).toBeTruthy();
    expect(within(row("bob@example.com")).getByText("$0.004")).toBeTruthy();
    await u.click(screen.getByRole("button", { name: "再取得" }));
    expect(api.getAdminCosts).toHaveBeenLastCalledWith(true);
  });

  it("still works when costs cannot be loaded", async () => {
    api.getAdminCosts.mockRejectedValueOnce(new ApiError(502, "X", "x"));
    render(<AdminView onAuthExpired={() => undefined} />);
    expect(await screen.findByText("alice@example.com")).toBeTruthy();
  });

  it("suspends an active key and resumes a held user", async () => {
    const CAROL = user({ sub: "c", email: "carol@example.com", status: "SUSPENDED", suspended: true });
    api.getAdminUsers.mockResolvedValue({ users: [ALICE, BOB, CAROL] });
    api.adminSuspend.mockResolvedValueOnce({ user: { ...ALICE, status: "SUSPENDED", suspended: true } });
    api.adminResume.mockResolvedValueOnce({ user: { ...CAROL, status: "ACTIVE", suspended: false } });
    const u = userEvent.setup();
    render(<AdminView onAuthExpired={() => undefined} />);
    await screen.findByText("carol@example.com");

    const carol = row("carol@example.com");
    expect(within(carol).getByRole("button", { name: "再開" })).toBeTruthy();
    expect(within(carol).getByRole("button", { name: "失効" })).toBeTruthy();
    expect(within(carol).queryByRole("button", { name: "一時停止" })).toBeNull();
    expect(within(carol).queryByRole("button", { name: "枠を解放" })).toBeNull();
    expect(within(row("bob@example.com")).queryByRole("button", { name: "一時停止" })).toBeNull();

    await u.click(within(row("alice@example.com")).getByRole("button", { name: "一時停止" }));
    expect(api.adminSuspend).toHaveBeenCalledWith("a");
    expect(await screen.findByText("alice@example.com のキーを一時停止しました。")).toBeTruthy();
    await u.click(within(row("carol@example.com")).getByRole("button", { name: "再開" }));
    expect(api.adminResume).toHaveBeenCalledWith("c");
  });

  it("marks a held user whose key is no longer suspended", async () => {
    api.getAdminUsers.mockResolvedValue({ users: [user({ sub: "d", email: "dan@example.com", status: "EXPIRED", suspended: true })] });
    render(<AdminView onAuthExpired={() => undefined} />);
    const dan = await screen.findByText("dan@example.com").then(() => row("dan@example.com"));
    expect(within(dan).getByText("停止中")).toBeTruthy();
    expect(within(dan).getByRole("button", { name: "再開" })).toBeTruthy();
  });

  it("changes the registration cap with validation and a warning when lowering below usage", async () => {
    api.adminSetRegistrationCap.mockResolvedValue({ registrationCap: 1, reservedSlots: 2 });
    const u = userEvent.setup();
    render(<AdminView onAuthExpired={() => undefined} />);
    await u.click(await screen.findByRole("button", { name: "上限を変更" }));
    const input = screen.getByLabelText("新しい登録枠");

    await u.clear(input);
    await u.type(input, "0");
    await u.click(screen.getByRole("button", { name: "保存" }));
    expect(api.adminSetRegistrationCap).not.toHaveBeenCalled();
    expect(screen.getByText("登録枠は 1〜1000 の整数で指定してください。")).toBeTruthy();

    await u.clear(input);
    await u.type(input, "1");
    await u.click(screen.getByRole("button", { name: "保存" }));
    expect(vi.mocked(window.confirm).mock.calls.at(-1)?.[0]).toMatch("現在の使用数（2）より小さい");
    expect(api.adminSetRegistrationCap).toHaveBeenCalledWith(1);
    expect(await screen.findByText("登録枠を 1 に変更しました。")).toBeTruthy();
  });

  it("sets keys to never expire when the ceiling allows it", async () => {
    api.adminSetCredentialAge.mockResolvedValueOnce({ credentialAgeDays: null });
    const u = userEvent.setup();
    render(<AdminView onAuthExpired={() => undefined} />);
    expect(await screen.findByText("7 日間")).toBeTruthy();
    await u.click(screen.getByRole("button", { name: "有効期限を変更" }));
    await u.click(screen.getByLabelText("無期限"));
    expect(screen.getByLabelText("有効日数")).toHaveProperty("disabled", true);
    await u.click(screen.getAllByRole("button", { name: "保存" })[0]);
    expect(vi.mocked(window.confirm).mock.calls.at(-1)?.[0]).toMatch("漏えいすると失効するまで");
    expect(api.adminSetCredentialAge).toHaveBeenCalledWith(null);
  });

  it("hides the unlimited option and enforces the ceiling when one is set", async () => {
    api.getAdminOverview.mockResolvedValue(overview({ maxCredentialAgeDays: 30 }));
    const u = userEvent.setup();
    render(<AdminView onAuthExpired={() => undefined} />);
    await u.click(await screen.findByRole("button", { name: "有効期限を変更" }));
    expect(screen.queryByLabelText("無期限")).toBeNull();
    const input = screen.getByLabelText("有効日数");
    await u.clear(input);
    await u.type(input, "31");
    await u.click(screen.getAllByRole("button", { name: "保存" })[0]);
    expect(api.adminSetCredentialAge).not.toHaveBeenCalled();
    expect(screen.getByText("有効期限は 1〜30 日の整数で指定してください。")).toBeTruthy();
  });

  it("invites a participant on an allowed domain", async () => {
    api.adminInviteUser.mockResolvedValueOnce({ email: "carol@example.com", resent: false });
    const u = userEvent.setup();
    render(<AdminView onAuthExpired={() => undefined} />);
    const input = await screen.findByLabelText("招待するメールアドレス");
    expect(screen.getByRole("button", { name: "招待メールを送る" })).toHaveProperty("disabled", true);
    await u.type(input, " Carol@Example.com ");
    await u.click(screen.getByRole("button", { name: "招待メールを送る" }));
    expect(api.adminInviteUser).toHaveBeenCalledWith("carol@example.com", false);
    expect(await screen.findByText(/carol@example.com に招待メールを送りました/)).toBeTruthy();
    expect(input).toHaveProperty("value", "");
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it("blocks other domains client-side and re-sends when asked", async () => {
    api.adminInviteUser.mockResolvedValueOnce({ email: "dan@example.com", resent: true });
    const u = userEvent.setup();
    render(<AdminView onAuthExpired={() => undefined} />);
    const input = await screen.findByLabelText("招待するメールアドレス");
    await u.type(input, "mallory@evil-example.com");
    expect(screen.getByRole("alert").textContent).toMatch("@example.com 以外のメールアドレスは登録できません");
    expect(screen.getByRole("button", { name: "招待メールを送る" })).toHaveProperty("disabled", true);

    await u.clear(input);
    await u.type(input, "dan@example.com");
    await u.click(screen.getByLabelText("招待メールを再送（初期パスワードを再発行）"));
    await u.click(screen.getByRole("button", { name: "招待メールを再送" }));
    expect(api.adminInviteUser).toHaveBeenCalledWith("dan@example.com", true);
    expect(await screen.findByText(/dan@example.com に招待メールを再送しました/)).toBeTruthy();
  });

  it("shows the server message when the address is already registered", async () => {
    api.adminInviteUser.mockRejectedValueOnce(new ApiError(409, "USER_EXISTS", "このメールアドレスは既に登録されています。"));
    const u = userEvent.setup();
    render(<AdminView onAuthExpired={() => undefined} />);
    await u.type(await screen.findByLabelText("招待するメールアドレス"), "alice@example.com");
    await u.click(screen.getByRole("button", { name: "招待メールを送る" }));
    expect(await screen.findByText("このメールアドレスは既に登録されています。")).toBeTruthy();
  });

  it("filters by email", async () => {
    const u = userEvent.setup();
    render(<AdminView onAuthExpired={() => undefined} />);
    await screen.findByText("alice@example.com");
    await u.type(screen.getByLabelText("メールアドレスで絞り込み"), "bob");
    expect(screen.queryByText("alice@example.com")).toBeNull();
    expect(screen.getByText("bob@example.com")).toBeTruthy();
  });

  it("force-revokes after confirmation and reloads", async () => {
    api.adminRevoke.mockResolvedValueOnce({ user: { ...ALICE, status: "REVOKED" } });
    const u = userEvent.setup();
    render(<AdminView onAuthExpired={() => undefined} />);
    await u.click(within(await screen.findByText("alice@example.com").then(() => row("alice@example.com"))).getByRole("button", { name: "失効" }));
    expect(window.confirm).toHaveBeenCalled();
    expect(api.adminRevoke).toHaveBeenCalledWith("a");
    expect(await screen.findByText("alice@example.com のキーを失効しました。")).toBeTruthy();
    expect(api.getAdminUsers).toHaveBeenCalledTimes(2);
  });

  it("does nothing when the confirmation is cancelled", async () => {
    vi.mocked(window.confirm).mockReturnValueOnce(false);
    const u = userEvent.setup();
    render(<AdminView onAuthExpired={() => undefined} />);
    await screen.findByText("bob@example.com");
    await u.click(within(row("bob@example.com")).getByRole("button", { name: "枠を解放" }));
    expect(api.adminReleaseSlot).not.toHaveBeenCalled();
  });

  it("toggles the kill switch", async () => {
    api.adminSetIssuance.mockResolvedValueOnce({ issuanceEnabled: false });
    const u = userEvent.setup();
    render(<AdminView onAuthExpired={() => undefined} />);
    await u.click(await screen.findByRole("button", { name: "発行を停止" }));
    expect(api.adminSetIssuance).toHaveBeenCalledWith(false);
  });

  it("shows server errors and handles expired sessions", async () => {
    api.adminRevoke.mockRejectedValueOnce(new ApiError(502, "AWS_API_ERROR", "AWS側の処理に失敗しました。"));
    const u = userEvent.setup();
    render(<AdminView onAuthExpired={() => undefined} />);
    await screen.findByText("alice@example.com");
    await u.click(within(row("alice@example.com")).getByRole("button", { name: "失効" }));
    expect(await screen.findByText("AWS側の処理に失敗しました。")).toBeTruthy();

    api.getAdminOverview.mockRejectedValueOnce(new ApiError(401, "UNAUTHENTICATED", "x"));
    const onAuthExpired = vi.fn();
    render(<AdminView onAuthExpired={onAuthExpired} />);
    await waitFor(() => expect(onAuthExpired).toHaveBeenCalled());
  });

  it("shows a forbidden error for non-admins", async () => {
    api.getAdminOverview.mockRejectedValueOnce(new ApiError(403, "FORBIDDEN", "この操作を行う権限がありません。"));
    render(<AdminView onAuthExpired={() => undefined} />);
    expect(await screen.findByText("この操作を行う権限がありません。")).toBeTruthy();
  });
});
