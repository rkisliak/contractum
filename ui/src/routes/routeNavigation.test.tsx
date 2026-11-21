// Routes that write their own search params back: the folder navigator's archived switch and the
// subscriber card's tabs, both through the real router.
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import { cardFixture } from "@/test/cards-fixtures";
import { http, HttpResponse } from "@/test/msw";
import { renderApp, ROOT_ID } from "@/test/renderApp";

afterEach(() => {
  tokenStore.clear();
});

describe("search params written back by a route", () => {
  it("drops archived from the folder navigator URL when the switch goes off", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/folders?archived=true");
    const archived = await screen.findByLabelText("Show archived");
    expect(archived).toBeChecked();
    await user.click(archived);
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
    await user.click(screen.getByLabelText("Show archived"));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ archived: true });
    });
  });

  it("keeps the open card tab in the URL, and none for Overview", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/cards/card-1?tab=members", {
      handlers: [
        http.get("*/api/v1/contracts/cards/:id", () =>
          HttpResponse.json(cardFixture({ folder_id: ROOT_ID })),
        ),
        http.get("*/api/v1/contracts/cards/:id/notices", () =>
          HttpResponse.json({ items: [], next_cursor: null }),
        ),
      ],
    });
    await user.click(await screen.findByRole("tab", { name: "Overview" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
    await user.click(screen.getByRole("tab", { name: "Notices" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ tab: "notices" });
    });
  });
});
