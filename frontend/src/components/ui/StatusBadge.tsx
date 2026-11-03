import type { HTMLAttributes, ReactNode } from "react";

export type StatusTone =
  | "info"
  | "success"
  | "warning"
  | "danger"
  | "neutral"
  | "proposed"
  | "stale"
  | "unavailable"
  | "synthetic"
  | "archived";

export type StatusBadgeProps = HTMLAttributes<HTMLSpanElement> & {
  children: ReactNode;
  tone: StatusTone;
};

function StatusBadge({ children, className, tone, ...props }: StatusBadgeProps) {
  const classes = ["ui-status-badge", `ui-status-badge--${tone}`, className].filter(Boolean).join(" ");
  return <span className={classes} {...props}>{children}</span>;
}

export default StatusBadge;
