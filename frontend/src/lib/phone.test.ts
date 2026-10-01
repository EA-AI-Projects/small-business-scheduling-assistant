import { isE164, normalizePhone } from "./phone";

describe("normalizePhone", () => {
  it("strips directional marks around a number", () => {
    expect(normalizePhone("‪+14155550101‬")).toBe("+14155550101");
    expect(normalizePhone("‎+1⁦415⁩5550101‎")).toBe("+14155550101");
  });
  it("strips NBSP and other whitespace", () => {
    expect(normalizePhone(" +1 415 555\t0101 ")).toBe("+14155550101");
  });
  it("strips separators", () => {
    expect(normalizePhone("+1 (415) 555-0101")).toBe("+14155550101");
    expect(normalizePhone("+1.415.555.0101")).toBe("+14155550101");
  });
  it("leaves invalid input invalid", () => {
    expect(isE164(normalizePhone("415-555-0101"))).toBe(false);
    expect(isE164(normalizePhone("+0 415 555 0101"))).toBe(false);
    expect(isE164(normalizePhone("+1415555010a"))).toBe(false);
    expect(isE164(normalizePhone(""))).toBe(false);
  });
  it("accepts a normalized valid number", () => {
    expect(isE164(normalizePhone("‪+1 (415) 555-0101‬"))).toBe(true);
  });
});
