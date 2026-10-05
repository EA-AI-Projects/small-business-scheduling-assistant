// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { ConfirmButton } from "./ConfirmButton";

let tab = "requests";
vi.mock("@/owner/OwnerContext", () => ({ useOwner: () => ({ tab }) }));

describe("ConfirmButton", () => {
  let host: HTMLDivElement;
  let root: Root;
  const onConfirm = vi.fn();

  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    tab = "requests";
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.useRealTimers();
  });

  async function render() {
    await act(async () => root.render(<ConfirmButton label="Approve" confirmation="Confirm approval" onConfirm={onConfirm} />));
    return host.querySelector("button")!;
  }

  async function click(button: HTMLButtonElement) {
    await act(async () => button.click());
  }

  it.each([390, 1280])("keeps two-step confirmation at %ipx width", async (width) => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
    const button = await render();
    await click(button);
    expect(button.textContent).toBe("Confirm approval");
    expect(onConfirm).not.toHaveBeenCalled();
    await click(button);
    expect(onConfirm).toHaveBeenCalledOnce();
    expect(button.textContent).toBe("Approve");
  });

  it("disarms on blur, Escape, section change, and timeout", async () => {
    const button = await render();
    await click(button);
    await act(async () => button.dispatchEvent(new FocusEvent("focusout", { bubbles: true })));
    expect(button.textContent).toBe("Approve");

    await click(button);
    await act(async () => button.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(button.textContent).toBe("Approve");

    await click(button);
    tab = "schedule";
    await act(async () => root.render(<ConfirmButton label="Approve" confirmation="Confirm approval" onConfirm={onConfirm} />));
    expect(button.textContent).toBe("Approve");

    await click(button);
    await act(async () => vi.advanceTimersByTime(5000));
    expect(button.textContent).toBe("Approve");
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
