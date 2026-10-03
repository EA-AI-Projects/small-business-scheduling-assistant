import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties,
  type ReactNode } from "react";
import { createPortal } from "react-dom";

import { placeBelow, placeCard, type Box } from "@/lib/popoverPlacement";

const GAP = 8;
const DROPDOWN_WIDTH = 20 * 16;
const CARD_WIDTH = 22 * 16;
const MODAL_WIDTH = 36 * 16;
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

// Cards open at once, and the page regions made inert for them. One owner for all cards, so a
// second card (or an overlapping unmount) can never leave `main` inert.
let openCards = 0;
let savedInert: [HTMLElement, boolean][] = [];

function lockBackground(): void {
  if (openCards === 0) {
    savedInert = [...document.querySelectorAll<HTMLElement>(BACKGROUND)].map((region) => [region, region.inert]);
  }
  openCards += 1;
  savedInert.forEach(([region]) => { region.inert = true; });
}

function unlockBackground(): void {
  openCards = Math.max(0, openCards - 1);
  if (openCards > 0) return;
  savedInert.forEach(([region, before]) => { region.inert = before; });
  savedInert = [];
}

/**
 * The real topmost element under a viewport point. Inert regions are skipped by hit-testing, so
 * clear `inert`, ask the browser, and restore it, all synchronously (no paint or event in between).
 * Sticky headings, the gutter, the app header and a pinned notice therefore win over an item under them.
 */
export function elementAt(x: number, y: number): Element | null {
  savedInert.forEach(([region]) => { region.inert = false; });
  try {
    return document.elementFromPoint(x, y);
  } finally {
    savedInert.forEach(([region]) => { region.inert = true; });
  }
}

/**
 * A pop-up card shared by the calendar item card and the empty-slot card.
 *
 * It is modal: the page behind it is inert, Tab stays inside, and Escape, the close button, or a
 * press outside close it. The press is hit-tested for the topmost element: one inside an anchor
 * element (`data-popover-anchor`) goes to `onAnchorPress` so the opener can switch items, and one
 * on a slot column (`data-slot-column`) goes to `onSlotPress`; anything else closes. On close, focus goes to `returnFocus`
 * unless the user already moved it to a control. `getAnchor` gives the rectangle to point at; on
 * phones the card is a bottom sheet instead. `variant="modal"` centres the card horizontally near
 * the top of the viewport with a dimmed backdrop (no anchor needed), for a form that grows and
 * shrinks; it is the same card, so the single inert owner, focus handling and sheet apply unchanged.
 * `refocusKey` changes when the content was rebuilt or
 * the card moved to another item; if that left focus outside the card, it returns to the card.
 */
export function PopoverCard({ title, variant = "anchored", getAnchor, onClose, onAnchorPress, onSlotPress, returnFocus, refocusKey, children }: {
  title: string;
  /**
   * "dropdown" opens directly under the anchor at a fixed width, on phones too (no bottom sheet),
   * with the title visually hidden and no close button (Escape, an outside press, or a pick closes it). Used by the header date picker.
   */
  variant?: "anchored" | "modal" | "dropdown";
  /** Not used by the modal variant. */
  getAnchor: () => CardAnchor | null;
  onClose: () => void;
  onAnchorPress?: (element: HTMLElement) => void;
  /** A press on an empty slot of a day column: the column and the viewport point. */
  onSlotPress?: (column: HTMLElement, x: number, y: number) => void;
  /** Element, or a function finding it at close time, that gets focus back. */
  returnFocus?: HTMLElement | null | (() => HTMLElement | null);
  refocusKey?: unknown;
  children: ReactNode;
}) {
  const headingId = useId();
  const card = useRef<HTMLDivElement>(null);
  const [style, setStyle] = useState<CSSProperties>({ opacity: 0 });
  const [phone, setPhone] = useState(false);
  const latest = useRef({ onClose, onAnchorPress, onSlotPress, returnFocus });
  useEffect(() => { latest.current = { onClose, onAnchorPress, onSlotPress, returnFocus }; });

  const reposition = useCallback(() => {
    const element = card.current;
    if (!element) return;
    const onPhone = variant !== "dropdown" && isPhone();
    setPhone(onPhone);
    const modal = variant === "modal";
    const dropdown = variant === "dropdown";
    const anchor = onPhone || modal ? null : getAnchor();
    if (!onPhone && !modal && !anchor) return;
    if (modal && !onPhone) {
      const gap = GAP;
      const viewport = { width: document.documentElement.clientWidth, height: window.innerHeight };
      const minTop = topLimit() + gap;
      const width = Math.min(MODAL_WIDTH, viewport.width - 2 * gap);
      const maxHeight = Math.max(viewport.height - minTop - gap, 120);
      const height = Math.min(element.offsetHeight, maxHeight);
      // A fixed fraction from the top, so opening a section grows the card downward instead of re-centring.
      const top = Math.min(Math.max(viewport.height * 0.08, minTop), Math.max(viewport.height - height - gap, minTop));
      const css: CSSProperties = { left: (viewport.width - width) / 2, top, width, maxHeight };
      setStyle((current) => JSON.stringify(current) === JSON.stringify(css) ? current : css);
      return;
    }
    const viewport = { width: document.documentElement.clientWidth, height: window.innerHeight };
    const next = anchor && dropdown ? placeBelow({ viewport, topLimit: topLimit(),
      anchor: anchor.getBoundingClientRect(), card: { width: DROPDOWN_WIDTH, height: element.offsetHeight }, gap: GAP })
      : anchor ? placeCard({
      viewport: { width: document.documentElement.clientWidth, height: window.innerHeight },
      topLimit: topLimit(), anchor: anchor.getBoundingClientRect(),
      card: { width: CARD_WIDTH, height: element.offsetHeight }, gap: GAP,
    }) : null;
    const css: CSSProperties = next
      ? { left: next.left, top: next.top, width: next.width, maxHeight: next.maxHeight } : {};
    // Keep the old object when nothing moved, so repositioning on every render cannot loop.
    setStyle((current) => JSON.stringify(current) === JSON.stringify(css) ? current : css);
  }, [getAnchor, variant]);

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
    lockBackground();
    return () => {
      unlockBackground();
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
      const hit = elementAt(event.clientX, event.clientY);
      // The pinned error notice (and its Dismiss) must not close the card and lose the owner's entry.
      if (hit?.closest(".notice.pinned")) return;
      const anchor = hit?.closest<HTMLElement>("[data-popover-anchor]");
      const column = hit?.closest<HTMLElement>("[data-slot-column]");
      if (anchor && latest.current.onAnchorPress) latest.current.onAnchorPress(anchor);
      else if (column && hit === column && latest.current.onSlotPress) {
        latest.current.onSlotPress(column, event.clientX, event.clientY);
      } else latest.current.onClose();
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
    <>
    {variant === "modal" && <div className="popover-scrim" aria-hidden="true" />}
    <div ref={card} role="dialog" aria-modal="true" aria-labelledby={headingId} tabIndex={-1}
      className={`popover-card${variant === "modal" ? " modal" : ""}${variant === "dropdown" ? " dropdown" : ""}${phone ? " sheet" : ""}`} style={style}
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        // Collapsed accordion bodies are hidden but still in the DOM; skip what cannot take focus.
        const items = [...(card.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])]
          .filter((item) => item.getClientRects().length > 0);
        const first = items[0];
        const last = items[items.length - 1];
        if (!first || !last) { event.preventDefault(); return; }
        const active = document.activeElement;
        if (event.shiftKey && (active === first || active === card.current)) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && active === last) { event.preventDefault(); first.focus(); }
      }}>
      {variant === "dropdown" ? (
        <>
          <h3 id={headingId} className="visually-hidden">{title}</h3>
        </>
      ) : (
        <div className="popover-head">
          <h3 id={headingId}>{title}</h3>
          <button type="button" className="icon-button popover-close" aria-label="Close" onClick={onClose}>
            <span aria-hidden="true">×</span>
          </button>
        </div>
      )}
      {children}
    </div>
    </>,
    document.body,
  );
}
