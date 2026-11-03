import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";

import "./ContextMenu.css";

// Progressive-disclosure primitive (161): rare actions live in a menu opened by
// right-click, the context-menu key / Shift+F10, or an explicit menu button, so
// every action keeps a keyboard and non-pointer route.

export type ContextMenuItem = Readonly<{
  id: string;
  label: string;
  onSelect(): void;
  disabled?: boolean;
  hint?: string;
}>;

type Point = Readonly<{ x: number; y: number }>;

type ContextMenuProps = Readonly<{
  label: string;
  items: readonly ContextMenuItem[];
  at: Point | null;
  onClose(): void;
  /** Opening control; pressing it while open is a toggle, not an outside click. */
  triggerRef?: { readonly current: HTMLElement | null };
}>;

export function ContextMenu({ label, items, at, onClose, triggerRef }: ContextMenuProps) {
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [position, setPosition] = useState<Point | null>(null);

  useLayoutEffect(() => {
    if (!at || !menuRef.current) { setPosition(null); return; }
    const { width, height } = menuRef.current.getBoundingClientRect();
    setPosition({
      x: Math.max(4, Math.min(at.x, window.innerWidth - width - 4)),
      y: Math.max(4, Math.min(at.y, window.innerHeight - height - 4)),
    });
  }, [at]);

  useLayoutEffect(() => {
    if (!position) return;
    menuRef.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
  }, [position]);

  useEffect(() => {
    if (!at) return;
    const close = (event: Event) => {
      if (menuRef.current && event.target instanceof Node && menuRef.current.contains(event.target)) return;
      if (triggerRef?.current && event.target instanceof Node && triggerRef.current.contains(event.target)) return;
      onClose();
    };
    window.addEventListener("pointerdown", close, true);
    window.addEventListener("resize", onClose);
    window.addEventListener("blur", onClose);
    return () => {
      window.removeEventListener("pointerdown", close, true);
      window.removeEventListener("resize", onClose);
      window.removeEventListener("blur", onClose);
    };
  }, [at, onClose, triggerRef]);

  if (!at) return null;

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const buttons = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? []);
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "Escape" || event.key === "Tab") { event.preventDefault(); event.stopPropagation(); onClose(); return; }
    if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Home" || event.key === "End") {
      event.preventDefault();
      const next = event.key === "Home" ? 0
        : event.key === "End" ? buttons.length - 1
        : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next]?.focus();
    }
  };

  return createPortal(
    <div
      ref={menuRef}
      className="ui-context-menu"
      role="menu"
      aria-label={label}
      style={{ left: position?.x ?? at.x, top: position?.y ?? at.y, visibility: position ? "visible" : "hidden" }}
      onKeyDown={onKeyDown}
      onContextMenu={(event) => event.preventDefault()}
    >
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          role="menuitem"
          disabled={item.disabled}
          title={item.hint}
          onClick={() => { onClose(); item.onSelect(); }}
        >
          {item.label}
        </button>
      ))}
    </div>,
    document.body
  );
}

/**
 * Wires one menu to a target: right-click, the context-menu key or Shift+F10
 * on the focused target, and `openFrom(element)` for an explicit menu button.
 * Focus returns to the element that opened the menu.
 */
export function useContextMenu() {
  const [at, setAt] = useState<Point | null>(null);
  const returnFocus = useRef<HTMLElement | SVGElement | null>(null);

  const close = useCallback(() => {
    setAt(null);
    const target = returnFocus.current;
    returnFocus.current = null;
    if (target?.isConnected) window.requestAnimationFrame(() => target.focus());
  }, []);

  const openFrom = useCallback((element: HTMLElement) => {
    const rect = element.getBoundingClientRect();
    returnFocus.current = element;
    setAt({ x: rect.left, y: rect.bottom + 2 });
  }, []);

  const onContextMenu = useCallback((event: MouseEvent<HTMLElement | SVGElement>) => {
    event.preventDefault();
    returnFocus.current = event.currentTarget;
    setAt({ x: event.clientX, y: event.clientY });
  }, []);

  const onKeyDown = useCallback((event: KeyboardEvent<HTMLElement | SVGElement>) => {
    if (event.key !== "ContextMenu" && !(event.key === "F10" && event.shiftKey)) return;
    event.preventDefault();
    const element = event.currentTarget;
    const rect = element.getBoundingClientRect();
    returnFocus.current = element;
    setAt({ x: rect.left + Math.min(24, rect.width / 2), y: rect.top + Math.min(24, rect.height / 2) });
  }, []);

  return { at, close, openFrom, targetProps: { onContextMenu, onKeyDown } };
}

type MenuButtonProps = Readonly<{
  label: string;
  items: readonly ContextMenuItem[];
  className?: string;
  children?: ReactNode;
  disabled?: boolean;
}>;

/** Explicit, always-visible route to a menu (the accessible equivalent of right-click). */
export function MenuButton({ label, items, className, children, disabled }: MenuButtonProps) {
  const menu = useContextMenu();
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  return <>
    <button
      ref={triggerRef}
      type="button"
      className={["ui-menu-button", className].filter(Boolean).join(" ")}
      aria-label={label}
      title={label}
      aria-haspopup="menu"
      aria-expanded={menu.at !== null}
      disabled={disabled}
      onClick={(event) => (menu.at ? menu.close() : menu.openFrom(event.currentTarget))}
    >
      {children ?? <span aria-hidden="true">⋯</span>}
    </button>
    <ContextMenu label={label} items={items} at={menu.at} onClose={menu.close} triggerRef={triggerRef} />
  </>;
}
