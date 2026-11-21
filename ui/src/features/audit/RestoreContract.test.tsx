// The restore panel of a contract.deleted audit event on its own: it reads the folder and name from
// whichever snapshot carries them, falls back to "The contract", and offers nothing when no
// snapshot names a folder.
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { AuditEvent } from "@/api/types";
import { renderWithApi } from "@/test/governance-hooks";
import { http, HttpResponse, server } from "@/test/msw";
import { RestoreContract } from "./RestoreContract";

function deletedEvent(
  before: AuditEvent["before"],
  after: AuditEvent["after"],
): AuditEvent {
  return {
    event_id: "e-del",
    occurred_at: "2026-11-15T09:00:00Z",
    received_at: "2026-11-15T09:00:01Z",
    service: "contracts",
    category: "change",
    aggregate_type: "contract",
    aggregate_id: "c-9",
    aggregate_version: 3,
    action: "contract.deleted",
    actor: { id: "local:u1", display: "Ada", via: "jwt" },
    request_id: null,
    scope_path: [],
    before,
    after,
    changes: [],
    detail: null,
  };
}

function allowRestore() {
  server.use(
    http.get("*/api/v1/identity/me/capabilities", () =>
      HttpResponse.json({
        folder_id: "team-1",
        roles: ["team_admin"],
        permissions: ["contract:restore"],
        inheritance_blocked: false,
        grantable_roles: [],
      }),
    ),
  );
}

describe("restore contract panel", () => {
  it("reads the folder from the later snapshot and names an unnamed contract generically", async () => {
    const user = userEvent.setup();
    allowRestore();
    server.use(
      http.post("*/api/v1/contracts/contracts/c-9/restore", () =>
        HttpResponse.json({ id: "c-9" }),
      ),
    );
    renderWithApi(
      <RestoreContract
        event={deletedEvent(null, { folder_id: "team-1", odcs_id: 7 })}
      />,
    );
    await user.click(
      await screen.findByRole("button", { name: "Restore contract" }),
    );
    expect(await screen.findByText("The contract was restored")).toBeVisible();
  });

  it("offers nothing when no snapshot names a folder", async () => {
    allowRestore();
    renderWithApi(
      <>
        <p>Event drawer</p>
        <RestoreContract
          event={deletedEvent({ folder_id: "" }, { odcs_id: "orders" })}
        />
      </>,
    );
    expect(await screen.findByText("Event drawer")).toBeVisible();
    expect(screen.queryByText("Deleted contract")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Restore contract" }),
    ).toBeNull();
  });
});
