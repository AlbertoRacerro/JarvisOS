import type { HTMLAttributes, ReactNode } from "react";

type SurfaceElement = "section" | "article" | "div";

export type SurfaceProps = HTMLAttributes<HTMLElement> & {
  as?: SurfaceElement;
  children: ReactNode;
};

function Surface({ as: Element = "section", className, children, ...props }: SurfaceProps) {
  const classes = ["ui-surface", className].filter(Boolean).join(" ");
  return <Element className={classes} {...props}>{children}</Element>;
}

export default Surface;
