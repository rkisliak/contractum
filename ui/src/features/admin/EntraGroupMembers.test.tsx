// The live Entra group members card on its own: client sorting on every column, the truncation
// note without a total, and the failures that are neither a 503 nor a 404.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { EntraGroupMember } from "@/api/types";
import { renderWithApi } from "@/test/governance-hooks";
import { http, HttpResponse, server } from "@/test/msw";
import { EntraGroupMembers } from "./EntraGroupMembers";

const MEMBERS = "*/api/v1/identity/entra/groups/reg-1/members";

function member(
  name: string,
  overrides: Partial<EntraGroupMember> = {},
): EntraGroupMember {
  return {
    display_name: name,
    object_id: `oid-${name}`,
    kind: "user",
    secondary: null,
    account_enabled: true,
    registered: false,
    record_id: null,
    principal_id: null,
    ...overrides,
  };
}

function names(): string[] {
  return screen.getAllByRole("rowheader").map((cell) => cell.textContent);
}

describe("Entra group members card", () => {
  it("sorts by each column and notes a cut without a total", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(MEMBERS, () =>
        HttpResponse.json({
          items: [
            member("Bea", {
              kind: "service_principal",
              secondary: "app-b",
              account_enabled: false,
            }),
            member("adam", { kind: "group", account_enabled: null }),
            member("Cleo", { kind: "other", secondary: "Cleo@example.com" }),
          ],
          truncated: true,
          total: null,
        }),
      ),
    );
    renderWithApi(<EntraGroupMembers registrationId="reg-1" />);
    expect(
      await screen.findByText("Showing the first 3 members."),
    ).toBeVisible();
    const table = screen.getByRole("table");
    const sortBy = async (header: string) => {
      await user.click(within(table).getByRole("button", { name: header }));
      return names();
    };
    expect(await sortBy("Name")).toEqual(["adam", "Bea", "Cleo"]);
    expect(await sortBy("Email or app ID")).toEqual(["adam", "Bea", "Cleo"]);
    expect(await sortBy("Type")).toEqual(["adam", "Cleo", "Bea"]);
    expect(await sortBy("Status")).toEqual(["Bea", "Cleo", "adam"]);
    expect(within(table).getByText("Not applicable")).toBeVisible();
    expect(within(table).getByText("Disabled")).toBeVisible();
    expect(within(table).getByText("Other")).toBeVisible();
  });

  it("shows a network failure as a problem with a retry", async () => {
    const user = userEvent.setup();
    let calls = 0;
    server.use(
      http.get(MEMBERS, () => {
        calls += 1;
        return HttpResponse.error();
      }),
    );
    renderWithApi(<EntraGroupMembers registrationId="reg-1" />);
    await user.click(
      await screen.findByRole("button", { name: "Retry loading" }),
    );
    await waitFor(() => {
      expect(calls).toBe(2);
    });
    expect(
      screen.queryByText(/Microsoft Entra ID could not be reached/),
    ).toBeNull();
  });

  it("treats an empty answer as a failure, never as no members", async () => {
    server.use(
      http.get(MEMBERS, () => new HttpResponse(null, { status: 204 })),
    );
    renderWithApi(<EntraGroupMembers registrationId="reg-1" />);
    expect(await screen.findByText("Something went wrong")).toBeVisible();
    expect(screen.queryByText("This group has no members")).toBeNull();
  });
});
