import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { parseConfig } from "@/lib/config";

import { OwnerSession } from "@/pages/index";

vi.mock("@/lib/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/auth")>()),
  completeSignIn: vi.fn(),
}));
import { completeSignIn } from "@/lib/auth";

const ORIGIN = "https://owner.example.com";
const cognito = parseConfig({
  authMode: "cognito", apiBaseUrl: "https://api.example.com", businessId: "pilot",
  cognitoDomain: "https://login.example.com", clientId: "public-client",
});
const local = parseConfig({ authMode: "local", apiBaseUrl: "http://127.0.0.1:8000", businessId: "pilot" });
const NOTICE = "Your session ended. Sign in again.";

let assign: ReturnType<typeof vi.fn>;
let fetched: number;

function stubApi401(message: string) {
  fetched = 0;
  vi.stubGlobal("fetch", vi.fn(async () => {
    fetched += 1;
    return new Response(JSON.stringify({ error: { code: "UNAUTHORIZED", message } }), { status: 401 });
  }));
}

beforeEach(() => {
  sessionStorage.clear();
  assign = vi.fn();
  vi.stubGlobal("location", { href: `${ORIGIN}/`, origin: ORIGIN, pathname: "/", search: "", assign });
  vi.mocked(completeSignIn).mockReset().mockResolvedValue(null);
});
afterEach(() => vi.unstubAllGlobals());

describe.each([
  ["non-owner", "Invalid owner identity"],
  ["expired token", "Token expired"],
])("owner API 401 (%s) with Cognito", (_name, message) => {
  it("ends the hosted UI session once, then shows the notice without auto sign-in", async () => {
    stubApi401(message);
    vi.mocked(completeSignIn).mockResolvedValueOnce("access-token");
    const first = render(<OwnerSession config={cognito} />);
    await waitFor(() => expect(assign).toHaveBeenCalledTimes(1));
    // Calendar, requests, clients and policy load in parallel and all return 401.
    await waitFor(() => expect(fetched).toBeGreaterThanOrEqual(4));
    expect(assign).toHaveBeenCalledTimes(1);
    const target = new URL(String(assign.mock.calls[0]?.[0]));
    expect(target.origin + target.pathname).toBe("https://login.example.com/logout");
    expect(target.searchParams.get("client_id")).toBe("public-client");
    expect(target.search).toContain("logout_uri=https%3A%2F%2Fowner.example.com%2F");
    first.unmount();

    // The next load, back from the hosted UI logout.
    vi.mocked(completeSignIn).mockResolvedValueOnce(null);
    render(<OwnerSession config={cognito} />);
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Sign in" })[0]).toBeInTheDocument();
    expect(assign).toHaveBeenCalledTimes(1);
  });
});

describe("owner API 401 in local auth mode", () => {
  it("ends the session without navigating", async () => {
    stubApi401("Invalid owner identity");
    render(<OwnerSession config={local} />);
    await waitFor(() => expect(screen.getByLabelText("Local owner token")).toBeInTheDocument());
    await userEvent.type(screen.getByLabelText("Local owner token"), "tok");
    await userEvent.click(screen.getByRole("button", { name: "Sign in locally" }));
    // The provider's own load error may replace the notice text; the session still ends.
    await waitFor(() => expect(screen.getByText("Signed out")).toBeInTheDocument());
    expect(screen.getByLabelText("Local owner token")).toBeInTheDocument();
    expect(assign).not.toHaveBeenCalled();
    expect(sessionStorage.length).toBe(0);
  });
});
