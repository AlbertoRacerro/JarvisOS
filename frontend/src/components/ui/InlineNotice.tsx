import type { HTMLAttributes, ReactNode } from "react";

export type InlineNoticeTone = "info" | "success" | "warning" | "danger" | "neutral";

export type InlineNoticeProps = HTMLAttributes<HTMLDivElement> & {
  children: ReactNode;
  tone?: InlineNoticeTone;
};

const TONE_LABELS: Record<InlineNoticeTone, string> = {
  info: "Information",
  success: "Success",
  warning: "Warning",
  danger: "Error",
  neutral: "Notice"
};

function InlineNotice({ children, className, tone = "info", ...props }: InlineNoticeProps) {
  const classes = ["ui-inline-notice", `ui-inline-notice--${tone}`, className].filter(Boolean).join(" ");
  return (
    <div className={classes} role={tone === "danger" ? "alert" : undefined} {...props}>
      <strong>{TONE_LABELS[tone]}:</strong>
      {children}
    </div>
  );
}

export default InlineNotice;
