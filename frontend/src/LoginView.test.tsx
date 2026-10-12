import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LoginView } from "./components/LoginView";
import { passwordProblems } from "./password";

const auth = vi.hoisted(() => ({
  signInWithPassword: vi.fn(),
  setNewPassword: vi.fn(),
  requestPasswordReset: vi.fn(),
  completePasswordReset: vi.fn(),
  register: vi.fn(),
  confirmRegistration: vi.fn(),
  resendRegistrationCode: vi.fn(),
}));
vi.mock("./auth", () => auth);

beforeEach(() => vi.clearAllMocks());

const GOOD = "CorrectHorse42x";

describe("LoginView", () => {
  it("signs in with email and password", async () => {
    auth.signInWithPassword.mockResolvedValueOnce("done");
    const onSignedIn = vi.fn();
    const user = userEvent.setup();
    render(<LoginView onSignedIn={onSignedIn} />);
    await user.type(screen.getByLabelText("メールアドレス"), " Alice@Example.com ");
    await user.type(screen.getByLabelText("パスワード"), "pw");
    await user.click(screen.getByRole("button", { name: "ログイン" }));
    expect(auth.signInWithPassword).toHaveBeenCalledWith("alice@example.com", "pw");
    expect(onSignedIn).toHaveBeenCalled();
  });

  it("forces a new password on first sign-in with the temporary password", async () => {
    auth.signInWithPassword.mockResolvedValueOnce("newPasswordRequired");
    auth.setNewPassword.mockResolvedValueOnce(undefined);
    const onSignedIn = vi.fn();
    const user = userEvent.setup();
    render(<LoginView onSignedIn={onSignedIn} />);
    await user.type(screen.getByLabelText("メールアドレス"), "alice@example.com");
    await user.type(screen.getByLabelText("パスワード"), "Temp-1234");
    await user.click(screen.getByRole("button", { name: "ログイン" }));

    const submit = await screen.findByRole("button", { name: "パスワードを設定してログイン" });
    await user.type(screen.getByLabelText("新しいパスワード"), "short");
    expect(screen.getByText(/12 文字以上（あと 7 文字）/)).toBeTruthy();
    expect(submit).toHaveProperty("disabled", true);

    await user.clear(screen.getByLabelText("新しいパスワード"));
    await user.type(screen.getByLabelText("新しいパスワード"), GOOD);
    await user.type(screen.getByLabelText("新しいパスワード（確認）"), GOOD);
    await user.click(submit);
    expect(auth.setNewPassword).toHaveBeenCalledWith(GOOD);
    expect(onSignedIn).toHaveBeenCalled();
  });

  it("resets a forgotten password with the emailed code", async () => {
    auth.requestPasswordReset.mockResolvedValueOnce(undefined);
    auth.completePasswordReset.mockResolvedValueOnce(undefined);
    const user = userEvent.setup();
    render(<LoginView onSignedIn={() => undefined} />);
    await user.click(screen.getByRole("button", { name: "パスワードを忘れた場合" }));
    await user.type(screen.getByLabelText("メールアドレス"), "alice@example.com");
    await user.click(screen.getByRole("button", { name: "確認コードを送信" }));
    expect(auth.requestPasswordReset).toHaveBeenCalledWith("alice@example.com");
    expect(await screen.findByText(/登録済みであれば、確認コードを送信しました/)).toBeTruthy();

    await user.type(screen.getByLabelText("確認コード"), "12a3456");
    await user.type(screen.getByLabelText("新しいパスワード"), GOOD);
    await user.type(screen.getByLabelText("新しいパスワード（確認）"), GOOD);
    await user.click(screen.getByRole("button", { name: "パスワードを再設定" }));
    expect(auth.completePasswordReset).toHaveBeenCalledWith("alice@example.com", "123456", GOOD);
    expect(await screen.findByText(/新しいパスワードでログインしてください/)).toBeTruthy();
  });

  it("shows a generic message for wrong credentials", async () => {
    auth.signInWithPassword.mockRejectedValueOnce(
      Object.assign(new Error("Incorrect username or password."), { name: "NotAuthorizedException" }),
    );
    const user = userEvent.setup();
    render(<LoginView onSignedIn={() => undefined} />);
    await user.type(screen.getByLabelText("メールアドレス"), "alice@example.com");
    await user.type(screen.getByLabelText("パスワード"), "wrong");
    await user.click(screen.getByRole("button", { name: "ログイン" }));
    expect(await screen.findByText("メールアドレスまたはパスワードが正しくありません。")).toBeTruthy();
  });
});

describe("self registration", () => {
  it("registers an allowed-domain address, confirms the code and signs in", async () => {
    auth.register.mockResolvedValueOnce(undefined);
    auth.confirmRegistration.mockResolvedValueOnce(true);
    const onSignedIn = vi.fn();
    const user = userEvent.setup();
    render(<LoginView onSignedIn={onSignedIn} allowedDomains={["example.com"]} />);
    await user.click(screen.getByRole("button", { name: "新規登録" }));
    expect(screen.getByText("登録できるのは @example.com のメールアドレスだけです")).toBeTruthy();
    await user.type(screen.getByLabelText("メールアドレス"), "Alice@Example.com");
    await user.type(screen.getByLabelText("新しいパスワード"), GOOD);
    await user.type(screen.getByLabelText("新しいパスワード（確認）"), GOOD);
    await user.click(screen.getByRole("button", { name: "登録する" }));
    expect(auth.register).toHaveBeenCalledWith("alice@example.com", GOOD);

    expect(await screen.findByText(/確認コードを送信しました/)).toBeTruthy();
    await user.type(screen.getByLabelText("確認コード"), "123456");
    await user.click(screen.getByRole("button", { name: "確認してログイン" }));
    expect(auth.confirmRegistration).toHaveBeenCalledWith("alice@example.com", "123456");
    expect(onSignedIn).toHaveBeenCalled();
  });

  it("blocks other domains (including look-alike subdomains) before calling Cognito", async () => {
    const user = userEvent.setup();
    render(<LoginView onSignedIn={() => undefined} allowedDomains={["example.com"]} />);
    await user.click(screen.getByRole("button", { name: "新規登録" }));
    for (const bad of ["mallory@evil-example.com", "mallory@sub.example.com", "mallory@example.org"]) {
      await user.clear(screen.getByLabelText("メールアドレス"));
      await user.type(screen.getByLabelText("メールアドレス"), bad);
      await user.type(screen.getByLabelText("新しいパスワード"), GOOD);
      await user.type(screen.getByLabelText("新しいパスワード（確認）"), GOOD);
      expect(screen.getByText("@example.com 以外のメールアドレスは登録できません")).toBeTruthy();
      expect(screen.getByRole("button", { name: "登録する" })).toHaveProperty("disabled", true);
      await user.clear(screen.getByLabelText("新しいパスワード"));
      await user.clear(screen.getByLabelText("新しいパスワード（確認）"));
    }
    expect(auth.register).not.toHaveBeenCalled();
  });

  it("sends unconfirmed users to the code step on sign-in", async () => {
    auth.signInWithPassword.mockResolvedValueOnce("confirmRequired");
    const user = userEvent.setup();
    render(<LoginView onSignedIn={() => undefined} />);
    await user.type(screen.getByLabelText("メールアドレス"), "alice@example.com");
    await user.type(screen.getByLabelText("パスワード"), "pw");
    await user.click(screen.getByRole("button", { name: "ログイン" }));
    expect(await screen.findByRole("button", { name: "確認してログイン" })).toBeTruthy();
  });

  it("maps the server-side domain rejection", async () => {
    const { authErrorMessage } = await import("./messages");
    const e = Object.assign(new Error("PreSignUp failed with error DOMAIN_NOT_ALLOWED."), {
      name: "UserLambdaValidationException",
    });
    expect(authErrorMessage(e)).toBe("このメールドメインでは登録できません。");
  });
});

describe("passwordProblems", () => {
  it("mirrors the Cognito policy", () => {
    expect(passwordProblems(GOOD)).toEqual([]);
    expect(passwordProblems("alllowercase1234")).toEqual(["英大文字"]);
    expect(passwordProblems("Ab1")).toEqual(["12文字以上"]);
  });
});
