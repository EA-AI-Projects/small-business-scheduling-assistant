import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties,
  type ReactNode } from "react";
import { createPortal } from "react-dom";

import { placeCard, type Box } from "@/lib/popoverPlacement";

const GAP = 8;
const CARD_WIDTH = 22 * 16;
const PHONE_QUERY = "(max-width: 520px)";
const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), "
  + "textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";
/** Page regions made inert while the card is open. The single live region sits outside them. */
const BACKGROUND = "main, footer";

/** Anything with a rectangle can anchor the card: an element, or a DOMRect-like object for an empty slot. */
export interface CardAnchor { getBoundingClientRect(): Box }

function isPhone(): boolean {
  return window.matchMedia?.(PHONE_QUERY).matches ?? false;
}

/** Lowest edge of the sticky header and the pinned notice: the card must stay below it. */
function topLimit(): number {
  let bottom = 0;
  for (const element of document.querySelectorAll(".app-header, .notice.pinned")) {
    bottom = Math.max(bottom, element.getBoundingClientRect().bottom);
  }
  return bottom;
}

/** The anchor element under a viewport point. Inert elements are not hit-tested, so look at rectangles. */
function anchorAt(x: number, y: number): HTMLElement | null {
  const anchors = [...document.querySelectorAll<HTMLElement>("[data-popover-anchor]")].reverse();
  return anchors.find((element) => {
    const r = element.getBoundingClientRect();
    if (x < r.left || x > r.right || y < r.top || y > r.bottom) return false;
    const clip = element.closest("[data-popover-clip]")?.getBoundingClientRect();
    return !clip || (x >= clip.left && x <= clip.right && y >= clip.top && y <= clip.bottom);
  }) ?? null;
}

/**
 * A pop-up card shared by the calendar item card and, later, the empty-slot card.
 *
 * It is modal: the page behind it is inert, Tab stays inside, and Escape, the close button, or a
 * press outside close it. A press on another anchor element (`data-popover-anchor`) goes to
 * `onAnchorPress` instead, so the opener can switch items. On close, focus goes to `returnFocus`
 * unless the user already moved it to a control. `getAnchor` gives the rectangle to point at; on
 * phones the card is a bottom sheet instead. `refocusKey` changes when the content was rebuilt or
 * the card moved to another item; if that left focus outside the card, it returns to the card.
 */
export function PopoverCard({ title, getAnchor, onClose, onAnchorPress, returnFocus, refocusKey, children }: {
  title: string;
  getAnchor: () => CardAnchor | null;
  onClose: () => void;
  onAnchorPress?: (element: HTMLElement) => void;
  /** Element, or a function finding it at close time, that gets focus back. */
  returnFocus?: HTMLElement | null | (() => HTMLElement | null);
  refocusKey?: unknown;
  children: ReactNode;
}) {
  const headingId = useId();
  const card = useRef<HTMLDivElement>(null);
  const [style, setStyle] = useState<CSSProperties>({ opacity: 0 });
  const [phone, setPhone] = useState(false);
  const latest = useRef({ onClose, onAnchorPress, returnFocus });
  useEffect(() => { latest.current = { onClose, onAnchorPress, returnFocus }; });

  const reposition = useCallback(() => {
    const element = card.current;
    if (!element) return;
    const onPhone = isPhone();
    setPhone(onPhone);
    const anchor = onPhone ? null : getAnchor();
    if (!onPhone && !anchor) return;
    const next = anchor ? placeCard({
      viewport: { width: document.documentElement.clientWidth, height: window.innerHeight },
      topLimit: topLimit(), anchor: anchor.getBoundingClientRect(),
      card: { width: CARD_WIDTH, height: element.offsetHeight }, gap: GAP,
    }) : null;
    const css: CSSProperties = next
      ? { left: next.left, top: next.top, width: next.width, maxHeight: next.maxHeight } : {};
    // Keep the old object when nothing moved, so repositioning on every render cannot loop.
    setStyle((current) => JSON.stringify(current) === JSON.stringify(css) ? current : css);
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

  // Modal: everything behind the card is inert while it is open, and focus returns afterwards.
  // Removed on any unmount: close, item gone, sign-out.
  useEffect(() => {
    const regions = [...document.querySelectorAll<HTMLElement>(BACKGROUND)];
    const before = regions.map((region) => region.inert);
    regions.forEach((region) => { region.inert = true; });
    return () => {
      regions.forEach((region, index) => { region.inert = before[index] ?? false; });
      const target = latest.current.returnFocus;
      // After the click's own focus change, which would otherwise drop focus on the page body.
      window.setTimeout(() => {
        const active = document.activeElement;
        if (active && active !== document.body && document.body.contains(active)) return;
        (typeof target === "function" ? target() : target)?.focus({ preventScroll: true });
      }, 0);
    };
  }, []);

  // Focus moves into the card on open, and back into it if rebuilt content or an item switch dropped it.
  useEffect(() => {
    const focusCard = () => {
      const element = card.current;
      if (element && !element.contains(document.activeElement)) element.focus({ preventScroll: true });
    };
    focusCard();
    const timer = window.setTimeout(focusCard, 0);
    return () => window.clearTimeout(timer);
  }, [refocusKey]);

  // Escape or a press outside closes the card.
  useEffect(() => {
    const down = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && card.current?.contains(target)) return;
      const anchor = anchorAt(event.clientX, event.clientY);
      if (anchor && latest.current.onAnchorPress) latest.current.onAnchorPress(anchor);
      else latest.current.onClose();
    };
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") latest.current.onClose(); };
    document.addEventListener("pointerdown", down);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("pointerdown", down);
      document.removeEventListener("keydown", key);
    };
  }, []);

  return createPortal(
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
    </div>,
    document.body,
  );
}
