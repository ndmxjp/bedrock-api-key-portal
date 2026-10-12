import {
  autoSignIn,
  confirmResetPassword,
  confirmSignIn,
  confirmSignUp,
  fetchAuthSession,
  resendSignUpCode,
  resetPassword,
  signIn,
  signOut,
  signUp,
} from "aws-amplify/auth";

/**
 * Users self-register with an allowed-domain email and password (the Pre sign-up trigger
 * rejects other domains), confirm the email with a code, then sign in with SRP (the password
 * itself is never sent to Cognito). Admin-invited users sign in with a temporary password and
 * must set a new one.
 */
export type SignInResult = "done" | "newPasswordRequired" | "resetRequired" | "confirmRequired";

function errName(e: unknown): string {
  return (e as { name?: string } | null)?.name ?? "";
}

function unexpected(step: string): Error {
  return Object.assign(new Error(step), { name: "UnexpectedSignInStep" });
}

export async function signInWithPassword(email: string, password: string): Promise<SignInResult> {
  try {
    const { nextStep } = await signIn({
      username: email,
      password,
      options: { authFlowType: "USER_SRP_AUTH" },
    });
    switch (nextStep.signInStep) {
      case "DONE":
        return "done";
      case "CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED":
        return "newPasswordRequired";
      case "RESET_PASSWORD":
        return "resetRequired";
      default:
        throw unexpected(nextStep.signInStep);
    }
  } catch (e) {
    if (errName(e) === "UserAlreadyAuthenticatedException") return "done";
    if (errName(e) === "PasswordResetRequiredException") return "resetRequired";
    if (errName(e) === "UserNotConfirmedException") {
      await resendSignUpCode({ username: email });
      return "confirmRequired";
    }
    throw e;
  }
}

/** Register; Cognito emails a confirmation code to prove the address. */
export async function register(email: string, password: string): Promise<void> {
  await signUp({
    username: email,
    password,
    options: { userAttributes: { email }, autoSignIn: { authFlowType: "USER_SRP_AUTH" } },
  });
}

/** Confirm the email. Returns true when auto sign-in succeeded, false if a manual sign-in is needed. */
export async function confirmRegistration(email: string, code: string): Promise<boolean> {
  const { nextStep } = await confirmSignUp({ username: email, confirmationCode: code });
  if (nextStep.signUpStep !== "COMPLETE_AUTO_SIGN_IN") return false;
  try {
    return (await autoSignIn()).isSignedIn;
  } catch {
    return false;
  }
}

export async function resendRegistrationCode(email: string): Promise<void> {
  await resendSignUpCode({ username: email });
}

/** Completes the first sign-in with the temporary password by setting a permanent one. */
export async function setNewPassword(newPassword: string): Promise<void> {
  const res = await confirmSignIn({ challengeResponse: newPassword });
  if (!res.isSignedIn) throw unexpected(res.nextStep.signInStep);
}

export async function requestPasswordReset(email: string): Promise<void> {
  await resetPassword({ username: email });
}

export async function completePasswordReset(email: string, code: string, newPassword: string): Promise<void> {
  await confirmResetPassword({ username: email, confirmationCode: code, newPassword });
}

/** Current ID token (refreshed by Amplify when needed), or null when signed out. */
export async function getIdToken(): Promise<string | null> {
  try {
    const session = await fetchAuthSession();
    return session.tokens?.idToken?.toString() ?? null;
  } catch {
    return null;
  }
}

export async function logout(): Promise<void> {
  try {
    await signOut();
  } catch {
    // ignore: local tokens are cleared regardless
  }
}
