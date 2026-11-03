import type { AnchorHTMLAttributes, MouseEvent, ReactNode } from "react";

export type NavigateOptions = Readonly<{ replace?: boolean }>;
export type Navigate = (href: string, options?: NavigateOptions) => void;

type AppLinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & {
  href: string;
  navigate: Navigate;
  children: ReactNode;
};

function AppLink({ href, navigate, onClick, target, download, children, ...props }: AppLinkProps) {
  const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event);
    if (event.defaultPrevented) return;
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (download !== undefined || (target && target !== "_self")) return;

    const destination = new URL(href, window.location.href);
    if (destination.origin !== window.location.origin) return;

    event.preventDefault();
    navigate(`${destination.pathname}${destination.search}${destination.hash}`);
  };

  return (
    <a href={href} target={target} download={download} onClick={handleClick} {...props}>
      {children}
    </a>
  );
}

export default AppLink;
