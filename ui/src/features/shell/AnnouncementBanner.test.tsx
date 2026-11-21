import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { http, HttpResponse } from "@/test/msw";
import { renderApp } from "@/test/renderApp";
import { bannerKey } from "./AnnouncementBanner";

// The dismissal key is stored in sessionStorage, so it must never change for an existing banner.
// These values were computed with the previous implementation (`| 0` wrap, then `>>> 0`).
describe("bannerKey", () => {
  it("keeps the 32-bit djb2 keys of three fixed banners", () => {
    expect(bannerKey({ severity: "info", text: "Maintenance tonight" })).toBe(
      "13998ee9",
    );
    expect(
      bannerKey({
        severity: "warning",
        text: "Planned downtime: Überführung ✓ 😀",
        starts_at: "2026-11-21T08:00:00Z",
      }),
    ).toBe("2ce75014");
    // 5000 characters: the hash wraps past 2^32 many times.
    expect(
      bannerKey({ severity: "info", text: "x".repeat(5000), starts_at: null }),
    ).toBe("5d6f0249");
  });

  it("changes with the severity, the start and the text", () => {
    const base = { severity: "info" as const, text: "a" };
    const keys = new Set([
      bannerKey(base),
      bannerKey({ ...base, severity: "warning" }),
      bannerKey({ ...base, starts_at: "2026-01-01T00:00:00Z" }),
      bannerKey({ ...base, text: "b" }),
    ]);
    expect(keys.size).toBe(4);
  });
});

describe("the announcement above the page", () => {
  const banner = {
    text: "Read-only window tonight",
    severity: "info" as const,
  };

  afterEach(() => {
    tokenStore.clear();
    window.sessionStorage.clear();
    vi.restoreAllMocks();
  });

  function showWithBanner() {
    renderApp("/", {
      handlers: [
        http.get("*/api/v1/identity/settings/public", () =>
          HttpResponse.json({
            banner: { ...banner, starts_at: null, ends_at: null },
            entra_default_tenant_id: null,
            entra_default_owner_folder_id: null,
          }),
        ),
      ],
    });
  }

  it("shows an info announcement and remembers its dismissal for the session", async () => {
    const actor = userEvent.setup();
    showWithBanner();
    expect(await screen.findByText("Read-only window tonight")).toBeVisible();
    // Info is not an alert role (only danger is).
    expect(screen.queryByRole("alert")).toBeNull();
    await actor.click(screen.getByRole("button", { name: "Dismiss alert" }));
    expect(screen.queryByText("Read-only window tonight")).toBeNull();
    expect(
      window.sessionStorage.getItem(`dc-banner-dismissed:${bannerKey(banner)}`),
    ).toBe("1");
  });

  it("stays hidden when this session already dismissed it", async () => {
    window.sessionStorage.setItem(
      `dc-banner-dismissed:${bannerKey(banner)}`,
      "1",
    );
    showWithBanner();
    expect(
      await screen.findByRole("heading", { level: 1 }, { timeout: 10_000 }),
    ).toBeVisible();
    expect(screen.queryByText("Read-only window tonight")).toBeNull();
  });

  it("still shows and dismisses it when session storage is blocked", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    const setItem = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new DOMException("blocked", "SecurityError");
      });
    const actor = userEvent.setup();
    showWithBanner();
    expect(await screen.findByText("Read-only window tonight")).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "Dismiss alert" }));
    await waitFor(() => {
      expect(screen.queryByText("Read-only window tonight")).toBeNull();
    });
    expect(setItem).toHaveBeenCalled();
  });
});
