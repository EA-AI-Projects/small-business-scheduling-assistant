import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties,
  type ReactNode } from "react";

const GAP = 8;
const CARD_WIDTH = 22 * 16;
const PHONE_QUERY = "(max-width: 520px)";
const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), "
  + "textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

function isPhone(): boolean {
  return window.matchMedia?.(PHONE_QUERY).matches ?? false;
}

/** Lowest edge of the sticky header and the pinned notice: the card must stay below it. */
function topLimit(): number {
  let bottom = 0;
  for (const element of document.querySelectorAll(".app-header, .notice.pinned")) {
    bottom = Math.max(bottom, element.getBoundingClientRect().bottom);
  }
  return bottom + GAP;
}

/** Place the card beside the anchor (right, else left), clamped inside the visible viewport. */
function place(anchor: DOMRect, height: number): CSSProperties {
  const vw = document.documentElement.clientWidth;
  const vh = window.innerHeight;
  const minTop = topLimit();
  const width = Math.min(CARD_WIDTH, vw - 2 * GAP);
  let left: number;
  if (anchor.right + GAP + width <= vw - GAP) left = anchor.right + GAP;
  else if (anchor.left - GAP - width >= GAP) left = anchor.left - GAP - width;
  else left = Math.min(Math.max(anchor.left, GAP), vw - width - GAP);
  const maxHeight = Math.max(vh - minTop - GAP, 120);
  const top = Math.min(Math.max(anchor.top, minTop), Math.max(vh - Math.min(height, maxHeight) - GAP, minTop));
  return { left, top, width, maxHeight };
}

/**
 * A pop-up card shared by the calendar item card and, later, the empty-slot card.
 *
 * It is modal: Tab stays inside, and Escape, the close button, or a press outside close it. The
 * opener returns focus to the item. `getAnchor` finds the item the card points at; on phones the
 * card is a bottom sheet instead. `refocusKey` changes when the content was rebuilt (for example
 * after a refresh); if that dropped focus, it returns to the card itself.
 */
export function PopoverCard({ title, getAnchor, onClose, refocusKey, children }: {
  title: string;
  getAnchor: () => HTMLElement | null;
  onClose: () => void;
  refocusKey?: unknown;
  children: ReactNode;
}) {
  const headingId = useId();
  const card = useRef<HTMLDivElement>(null);
  const [style, setStyle] = useState<CSSProperties>({ opacity: 0 });
  const [phone, setPhone] = useState(false);

  const reposition = useCallback(() => {
    const element = card.current;
    if (!element) return;
    const onPhone = isPhone();
    setPhone(onPhone);
    const anchor = onPhone ? null : getAnchor();
    if (!onPhone && !anchor) return;
    const next = anchor ? place(anchor.getBoundingClientRect(), element.offsetHeight) : {};
    // Keep the old object when nothing moved, so repositioning on every render cannot loop.
    setStyle((current) => JSON.stringify(current) === JSON.stringify(next) ? current : next);
  }, [getAnchor]);

  useLayoutEffect(() => { reposition(); });
  useEffect(() => {
    const element = card.current;
    const options = { capture: true, passive: true } as const;
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, options);
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(reposition);
    if (element) resize?.observe(element);
    // The header and notice heights are published as style properties on the root element.
    const mutation = new MutationObserver(reposition);
    mutation.observe(document.documentElement, { attributes: true, attributeFilter: ["style"] });
    return () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, options);
      resize?.disconnect();
      mutation.disconnect();
    };
  }, [reposition]);

  // Focus moves into the card on open.
  useEffect(() => { card.current?.focus({ preventScroll: true }); }, []);
  // Rebuilt content can drop the focused control; keep focus inside the card.
  useEffect(() => {
    const element = card.current;
    if (element && !element.contains(document.activeElement)) element.focus({ preventScroll: true });
  }, [refocusKey]);

  // Escape or a press outside closes the card. A press on a calendar item is handled by the opener (switch or toggle).
  useEffect(() => {
    const down = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Element) || card.current?.contains(target)) return;
      if (target.closest("[data-popover-anchor]")) return;
      onClose();
    };
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    document.addEventListener("pointerdown", down);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("pointerdown", down);
      document.removeEventListener("keydown", key);
    };
  }, [onClose]);

  return (
    <div ref={card} role="dialog" aria-modal="true" aria-labelledby={headingId} tabIndex={-1}
      className={`popover-card${phone ? " sheet" : ""}`} style={style}
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        const items = [...(card.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])];
        const first = items[0];
        const last = items[items.length - 1];
        if (!first || !last) { event.preventDefault(); return; }
        const active = document.activeElement;
        if (event.shiftKey && (active === first || active === card.current)) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && active === last) { event.preventDefault(); first.focus(); }
      }}>
      <div className="popover-head">
        <h3 id={headingId}>{title}</h3>
        <button type="button" className="icon-button popover-close" aria-label="Close" onClick={onClose}>
          <span aria-hidden="true">×</span>
        </button>
      </div>
      {children}
    </div>
  );
}
