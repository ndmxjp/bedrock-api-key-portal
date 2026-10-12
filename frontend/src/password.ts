/** Mirrors the Cognito password policy (12+ chars, upper, lower, digit). */
export function passwordProblems(pw: string): string[] {
  const problems: string[] = [];
  if (pw.length < 12) problems.push("12文字以上");
  if (!/[a-z]/.test(pw)) problems.push("英小文字");
  if (!/[A-Z]/.test(pw)) problems.push("英大文字");
  if (!/[0-9]/.test(pw)) problems.push("数字");
  return problems;
}
