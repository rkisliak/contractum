// The MSAL redirect bridge entry (redirect.html): it hands the IdP response on as soon as it loads
// and logs, never throws, when that fails.
import { afterEach, describe, expect, it, vi } from "vitest";

const bridge = vi.hoisted(() => ({
  broadcastResponseToMainFrame: vi.fn<() => Promise<void>>(),
}));

vi.mock("@azure/msal-browser/redirect-bridge", () => bridge);

afterEach(() => {
  vi.resetModules();
  vi.restoreAllMocks();
  bridge.broadcastResponseToMainFrame.mockReset();
});

describe("the redirect bridge entry", () => {
  it("broadcasts the response to the main frame on load", async () => {
    bridge.broadcastResponseToMainFrame.mockResolvedValue(undefined);
    const logged = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    await import("@/redirect");
    expect(bridge.broadcastResponseToMainFrame).toHaveBeenCalledOnce();
    expect(logged).not.toHaveBeenCalled();
  });

  it("logs a failed hand-off to the console", async () => {
    const failure = new Error("no opener");
    bridge.broadcastResponseToMainFrame.mockRejectedValue(failure);
    const logged = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    await import("@/redirect");
    await vi.waitFor(() => {
      expect(logged).toHaveBeenCalledWith(failure);
    });
  });
});
