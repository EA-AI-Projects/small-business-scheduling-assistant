import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";

export interface MenuItem {
  id: string;
  label: string;
  /** Short count shown beside the label, e.g. pending requests. */
  badge?: number;
}

/**
 * Shared app header: menu button, app name, then caller-supplied controls. The compact menu
 * lists the sections and any footer content. Not owner-specific, so a
 * later client-facing shell can reuse it.
 */
export function AppHeader({ appName, items, current, onSelect, badgeTotal, trailing, children }: {
  appName: string;
  items: MenuItem[];
  current: string;
  onSelect: (id: string) => void;
  /** Count surfaced on the menu button while the menu is closed. */
  badgeTotal?: number;
  /** Rendered at the far right of the header, e.g. the account button. */
  trailing?: ReactNode;
  children?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setOpen(false); button.current?.focus(); }
    };
    const onPointer = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
    };
  }, [open]);

  // The header is sticky; publish its height so the pinned notice, the menu panel and the
  // document's scroll padding all sit below it.
  const header = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const element = header.current;
    const root = document.documentElement;
    if (!element) return;
    const apply = () => root.style.setProperty("--header-height", `${element.offsetHeight}px`);
    apply();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(apply);
    observer?.observe(element);
    window.addEventListener("resize", apply);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", apply);
      root.style.removeProperty("--header-height");
    };
  }, []);

  const pending = badgeTotal ?? 0;
  return (
    <header className="app-header" ref={header}>
      <div className="app-header-menu" ref={root} onBlur={(event) => {
          // Close when focus moves out of the menu (button and panel).
          const next = event.relatedTarget;
          if (open && next && !event.currentTarget.contains(next)) setOpen(false);
        }}>
        <button ref={button} type="button" className="icon-button" aria-expanded={open}
          aria-controls={menuId} aria-label={pending > 0 ? `Menu, ${pending} pending` : "Menu"}
          onClick={() => setOpen((value) => !value)}>
          <span className="hamburger" aria-hidden="true" />
          {pending > 0 && !open && <span className="dot" aria-hidden="true" />}
        </button>
        <nav id={menuId} className="menu-panel" aria-label="Sections" hidden={!open}>
          {items.map((item) => (
            <button key={item.id} type="button" className="menu-item"
              aria-current={item.id === current ? "page" : undefined}
              onClick={() => { onSelect(item.id); setOpen(false); button.current?.focus(); }}>
              <span>{item.label}</span>
              {item.badge !== undefined && item.badge > 0 && (
                <span className="badge" aria-label={`${item.badge} pending`}>{item.badge}</span>
              )}
            </button>
          ))}
        </nav>
      </div>
      <h1 className="app-name">{appName}</h1>
      {children}
      {trailing}
    </header>
  );
}
