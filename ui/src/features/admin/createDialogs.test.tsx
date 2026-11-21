// The two shared create dialogs of the admin screens on their own: CreateOwnedDialog (local group,
// service account) and CreateCredentialDialog (PAT, service-account key). Each refusal the API can
// give lands where the user can act on it: the field, the alert, or the fixed copy.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type Problem } from "@/api/problem";
import { renderWithApi } from "@/test/governance-hooks";
import { http, HttpResponse, server } from "@/test/msw";
import { makeMe } from "@/test/renderApp";
import { CreateCredentialDialog } from "./CreateCredentialDialog";
import { CreateOwnedDialog } from "./CreateOwnedDialog";

function refusal(
  status: number,
  slug: string | null,
  overrides: Partial<Extract<Problem, { kind: "problem" }>> = {},
): ApiError {
  return new ApiError({
    kind: "problem",
    status,
    slug,
    title: `Refused ${String(status)}`,
    detail: `${slug ?? "untyped"} detail`,
    errors: [],
    requestId: null,
    ...overrides,
  });
}

function mountOwned(
  create: (body: { name: string; owner_folder_id: string }) => Promise<{
    id: string;
  }>,
  owners = [{ value: "acme-id", label: "acme" }],
) {
  const onOpenChange = vi.fn();
  const onCreated = vi.fn();
  renderWithApi(
    <CreateOwnedDialog
      open
      onOpenChange={onOpenChange}
      owners={owners}
      idPrefix="group"
      title="Create local group"
      description="Groups collect users."
      emptyNameMessage="Enter a group name."
      conflictMessage="A group with this name already exists here."
      create={create}
      onCreated={onCreated}
    />,
  );
  return { onOpenChange, onCreated };
}

async function submitOwned(name: string) {
  const user = userEvent.setup();
  const dialog = await screen.findByRole("dialog", {
    name: "Create local group",
  });
  await user.type(within(dialog).getByLabelText("Name"), name);
  await user.click(
    within(dialog).getByRole("button", { name: "Create local group" }),
  );
  return { user, dialog };
}

describe("create owned record dialog", () => {
  it("resets when closed with Escape", async () => {
    const { onOpenChange } = mountOwned(() =>
      Promise.reject(refusal(500, "broken")),
    );
    const { user, dialog } = await submitOwned("ledger");
    expect(await within(dialog).findByText("broken detail")).toBeVisible();
    await user.keyboard("{Escape}");
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(within(dialog).getByLabelText("Name")).toHaveValue("");
  });

  it("asks for an owner when the caller may own nothing, and sends nothing", async () => {
    const create = vi.fn();
    mountOwned(create, []);
    const { dialog } = await submitOwned("ledger");
    expect(await within(dialog).findByText("Choose an owner.")).toBeVisible();
    expect(create).not.toHaveBeenCalled();
  });

  it("puts a 409 on the name field", async () => {
    mountOwned(() => Promise.reject(refusal(409, "conflict")));
    const { dialog } = await submitOwned("ledger");
    expect(
      await within(dialog).findByText(
        "A group with this name already exists here.",
      ),
    ).toBeVisible();
    expect(within(dialog).queryByText("conflict detail")).toBeNull();
  });

  it("maps field errors onto the fields without an alert", async () => {
    mountOwned(() =>
      Promise.reject(
        refusal(422, "validation-failed", {
          errors: [
            { pointer: "/owner_folder_id", detail: "Owner is archived." },
          ],
        }),
      ),
    );
    const { dialog } = await submitOwned("ledger");
    expect(await within(dialog).findByText("Owner is archived.")).toBeVisible();
    expect(within(dialog).queryByText("validation-failed detail")).toBeNull();
  });

  it("shows any other refusal in the alert, and Go back resets and closes", async () => {
    const { onOpenChange, onCreated } = mountOwned(() =>
      Promise.reject(refusal(500, "broken")),
    );
    const { user, dialog } = await submitOwned("ledger");
    expect(await within(dialog).findByText("broken detail")).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Go back" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(within(dialog).getByLabelText("Name")).toHaveValue("");
    expect(within(dialog).queryByText("broken detail")).toBeNull();
    expect(onCreated).not.toHaveBeenCalled();
  });
});

function mountCredential(create: () => Promise<string>) {
  server.use(
    http.get("*/api/v1/identity/me", () => HttpResponse.json(makeMe())),
    http.get("*/api/v1/identity/settings/public", () =>
      HttpResponse.json({
        banner: null,
        entra_default_tenant_id: null,
        entra_default_owner_folder_id: null,
        default_odcs_api_version: "v3.2.0",
        require_lifecycle_reason: false,
        credential_max_ttl_days: 365,
        password_rules: {
          min_length: 12,
          require_upper: false,
          require_lower: false,
          require_digit: false,
          require_symbol: false,
        },
      }),
    ),
  );
  const onOpenChange = vi.fn();
  renderWithApi(
    <CreateCredentialDialog
      open
      onOpenChange={onOpenChange}
      title="Create key"
      description="A key for the pipeline."
      submitLabel="Create key"
      defaultDays={90}
      create={create}
      onCreated={vi.fn()}
      refusalCopy={{ "credential-limit-reached": "This account has 5 keys." }}
    />,
  );
  return { onOpenChange };
}

async function submitCredential() {
  const user = userEvent.setup();
  const dialog = await screen.findByRole("dialog", { name: "Create key" });
  await user.type(within(dialog).getByLabelText("Name"), "nightly");
  await user.click(within(dialog).getByRole("button", { name: "Create key" }));
  return { user, dialog };
}

describe("create credential dialog", () => {
  it("says the expiry is out of range when the API gives no detail", async () => {
    mountCredential(() =>
      Promise.reject(refusal(422, "expiry-out-of-range", { detail: null })),
    );
    const { dialog } = await submitCredential();
    expect(
      await within(dialog).findByText("This expiry is out of range."),
    ).toBeVisible();
  });

  it("replaces a known refusal with its fixed copy and shows an unknown one as sent", async () => {
    let answer = refusal(409, "credential-limit-reached");
    mountCredential(() => Promise.reject(answer));
    const { user, dialog } = await submitCredential();
    expect(
      await within(dialog).findByText("This account has 5 keys."),
    ).toBeVisible();
    answer = refusal(403, "not-allowed-here");
    await user.click(
      within(dialog).getByRole("button", { name: "Create key" }),
    );
    expect(
      await within(dialog).findByText("not-allowed-here detail"),
    ).toBeVisible();
    answer = refusal(500, null);
    await user.click(
      within(dialog).getByRole("button", { name: "Create key" }),
    );
    expect(await within(dialog).findByText("untyped detail")).toBeVisible();
  });

  it("resets when closed with Escape", async () => {
    const { onOpenChange } = mountCredential(() =>
      Promise.reject(refusal(500, "broken")),
    );
    const { user, dialog } = await submitCredential();
    expect(await within(dialog).findByText("broken detail")).toBeVisible();
    await user.keyboard("{Escape}");
    expect(onOpenChange).toHaveBeenCalledWith(false);
    await waitFor(() => {
      expect(within(dialog).getByLabelText("Name")).toHaveValue("");
    });
  });

  it("resets and closes on Go back", async () => {
    const { onOpenChange } = mountCredential(() =>
      Promise.reject(refusal(500, "broken")),
    );
    const { user, dialog } = await submitCredential();
    expect(await within(dialog).findByText("broken detail")).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Go back" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    await waitFor(() => {
      expect(within(dialog).getByLabelText("Name")).toHaveValue("");
    });
    expect(within(dialog).queryByText("broken detail")).toBeNull();
  });
});
