import { Amplify } from "aws-amplify";
import { cognitoUserPoolsTokenProvider } from "aws-amplify/auth/cognito";
import { sessionStorage } from "aws-amplify/utils";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { loadRuntimeConfig } from "./config";
import "./styles.css";

async function boot() {
  const root = createRoot(document.getElementById("root") as HTMLElement);
  try {
    const cfg = await loadRuntimeConfig();
    Amplify.configure({
      Auth: { Cognito: { userPoolId: cfg.userPoolId, userPoolClientId: cfg.userPoolClientId, loginWith: { email: true } } },
    });
    // Tokens live only for this tab session (cleared when the tab closes).
    cognitoUserPoolsTokenProvider.setKeyValueStorage(sessionStorage);
    root.render(
      <StrictMode>
        <App allowedDomains={cfg.allowedDomains ?? []} />
      </StrictMode>,
    );
  } catch (e) {
    root.render(<p className="boot-error">{e instanceof Error ? e.message : "起動に失敗しました。"}</p>);
  }
}

void boot();
