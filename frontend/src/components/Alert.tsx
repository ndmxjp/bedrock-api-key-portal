import type { ReactNode } from "react";

type Tone = "info" | "warning" | "error" | "success";

export function Alert({ tone = "info", title, children }: { tone?: Tone; title?: string; children?: ReactNode }) {
  return (
    <div className={`alert alert-${tone}`} role={tone === "error" ? "alert" : "status"}>
      {title && <p className="alert-title">{title}</p>}
      {children && <div className="alert-body">{children}</div>}
    </div>
  );
}
