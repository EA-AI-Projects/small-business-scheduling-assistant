export interface Box { left: number; right: number; top: number; bottom: number }

export interface PlacementInput {
  viewport: { width: number; height: number };
  /** Lowest edge (px from the viewport top) of the sticky header and any pinned notice. */
  topLimit: number;
  anchor: Box;
  /** Natural card size; the width is shrunk to fit narrow viewports. */
  card: { width: number; height: number };
  gap?: number;
}

export interface Placement {
  left: number;
  top: number;
  width: number;
  maxHeight: number;
  /** Where the card sits relative to the anchor. "overlap" means neither side had room. */
  side: "right" | "left" | "overlap";
}

const MIN_MAX_HEIGHT = 120;

/** Place a card beside its anchor (right, else left), kept inside the viewport and below the top limit. */
export function placeCard({ viewport, topLimit, anchor, card, gap = 8 }: PlacementInput): Placement {
  const minTop = topLimit + gap;
  const width = Math.min(card.width, viewport.width - 2 * gap);
  let left: number;
  let side: Placement["side"];
  if (anchor.right + gap + width <= viewport.width - gap) {
    left = anchor.right + gap;
    side = "right";
  } else if (anchor.left - gap - width >= gap) {
    left = anchor.left - gap - width;
    side = "left";
  } else {
    left = Math.min(Math.max(anchor.left, gap), viewport.width - width - gap);
    side = "overlap";
  }
  const maxHeight = Math.max(viewport.height - minTop - gap, MIN_MAX_HEIGHT);
  const height = Math.min(card.height, maxHeight);
  const top = Math.min(Math.max(anchor.top, minTop), Math.max(viewport.height - height - gap, minTop));
  return { left, top, width, maxHeight, side };
}
