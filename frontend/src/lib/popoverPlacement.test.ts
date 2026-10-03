import { describe, expect, it } from "vitest";

import { placeCard } from "./popoverPlacement";

const viewport = { width: 1440, height: 900 };
const card = { width: 352, height: 300 };

describe("placeCard", () => {
  it("sits to the right of the anchor when there is room", () => {
    const p = placeCard({ viewport, topLimit: 55, anchor: { left: 166, right: 330, top: 461, bottom: 540 }, card });
    expect(p).toMatchObject({ side: "right", left: 338, top: 461, width: 352 });
  });

  it("flips to the left in the last (Sunday) column", () => {
    const p = placeCard({ viewport, topLimit: 55, anchor: { left: 1161, right: 1326, top: 461, bottom: 540 }, card });
    expect(p.side).toBe("left");
    expect(p.left).toBe(1161 - 8 - 352);
    expect(p.left + p.width).toBeLessThanOrEqual(1161);
  });

  it("overlaps and clamps when the anchor is as wide as the Day view", () => {
    const p = placeCard({ viewport: { width: 700, height: 900 }, topLimit: 55,
      anchor: { left: 60, right: 690, top: 300, bottom: 400 }, card });
    expect(p.side).toBe("overlap");
    expect(p.left).toBeGreaterThanOrEqual(8);
    expect(p.left + p.width).toBeLessThanOrEqual(700 - 8);
  });

  it("shrinks the card on a narrow viewport", () => {
    const p = placeCard({ viewport: { width: 300, height: 700 }, topLimit: 50,
      anchor: { left: 20, right: 100, top: 200, bottom: 260 }, card });
    expect(p.width).toBe(284);
    expect(p.left).toBe(8);
  });

  it("stays below the header and pinned notice", () => {
    const p = placeCard({ viewport, topLimit: 105, anchor: { left: 166, right: 330, top: 20, bottom: 80 }, card });
    expect(p.top).toBe(113);
  });

  it("is pulled up so the card ends above the bottom edge", () => {
    const p = placeCard({ viewport, topLimit: 55, anchor: { left: 166, right: 330, top: 850, bottom: 890 }, card });
    expect(p.top).toBe(900 - 300 - 8);
  });

  it("caps a tall card to the space under the limit", () => {
    const p = placeCard({ viewport: { width: 1440, height: 400 }, topLimit: 55,
      anchor: { left: 166, right: 330, top: 100, bottom: 160 }, card: { width: 352, height: 900 } });
    expect(p.maxHeight).toBe(400 - 63 - 8);
    expect(p.top).toBe(63);
  });
});
