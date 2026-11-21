import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { createApiClient } from "@/api/client";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import type { RuntimeConfig } from "@/config";
import { http, HttpResponse, server } from "@/test/msw";

// A consumer-shaped harness: a button opens the dialog, and confirming archives a folder through
// the generated client against msw, so a failure arrives as a real ApiError.
const config: RuntimeConfig = {
  entraEnabled: false,
  entraClientId: "",
  entraAuthority: "",
  entraScopes: [],
  entraProtocol: "oidc",
  localLoginEnabled: true,
  apiBase: "/api/v1",
};

const ARCHIVE = "/api/v1/identity/folders/f-1/archive";

type HarnessProps = {
  objectName?: string;
  typeToConfirm?: string;
  title?: string;
  onDone?: () => void;
};

function Harness({
  objectName = "Payments",
  typeToConfirm,
  title = "Archive Payments?",
  onDone,
}: HarnessProps) {
  const [open, setOpen] = useState(false);
  const api = createApiClient(config);
  return (
    <>
      <button
        type="button"
        onClick={() => {
          setOpen(true);
        }}
      >
        Archive folder
      </button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={title}
        body="It and everything below become read-only. Reads keep working."
        objectName={objectName}
        confirmLabel="Archive Payments"
        typeToConfirm={typeToConfirm}
        busyLabel="Archiving…"
        onConfirm={async () => {
          await api.POST("/api/v1/identity/folders/{folder_id}/archive", {
            params: { path: { folder_id: "f-1" } },
          });
          onDone?.();
        }}
      />
    </>
  );
}

async function openDialog() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Archive folder" }));
  const dialog = await screen.findByRole("alertdialog");
  return { user, dialog };
}

describe("ConfirmDialog", () => {
  it("names the action, focuses the dismiss button and never offers Cancel", async () => {
    render(<Harness />);
    const { dialog } = await openDialog();

    expect(
      within(dialog).getByRole("heading", { name: "Archive Payments?" }),
    ).toBeInTheDocument();
    expect(dialog).toHaveAccessibleDescription(
      "It and everything below become read-only. Reads keep working.",
    );
    const keep = within(dialog).getByRole("button", { name: "Keep Payments" });
    await waitFor(() => {
      expect(keep).toHaveFocus();
    });
    expect(screen.queryByRole("button", { name: /cancel/i })).toBeNull();
    expect(
      within(dialog).getByRole("button", { name: "Archive Payments" }),
    ).toBeEnabled();
  });

  it("falls back to Go back when no object applies", async () => {
    function NoObject() {
      return (
        <ConfirmDialog
          open
          onOpenChange={() => undefined}
          title="Discard changes?"
          body="Unsaved edits are lost."
          confirmLabel="Discard changes"
          onConfirm={() => Promise.resolve()}
        />
      );
    }
    render(<NoObject />);
    const dialog = await screen.findByRole("alertdialog");
    expect(
      within(dialog).getByRole("button", { name: "Go back" }),
    ).toBeInTheDocument();
  });

  it("shows the busy label while pending and closes on success", async () => {
    let release: () => void = () => undefined;
    server.use(
      http.post(ARCHIVE, async () => {
        await new Promise<void>((done) => {
          release = done;
        });
        return HttpResponse.json({});
      }),
    );
    const onDone = vi.fn();
    render(<Harness onDone={onDone} />);
    const { user, dialog } = await openDialog();

    await user.click(
      within(dialog).getByRole("button", { name: "Archive Payments" }),
    );
    const busy = await within(dialog).findByRole("button", {
      name: /Archiving/,
    });
    expect(busy).toHaveAttribute("aria-busy", "true");
    expect(busy).toHaveFocus();
    // The dismiss button and Esc are inert while the action runs.
    expect(
      within(dialog).getByRole("button", { name: "Keep Payments" }),
    ).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();

    release();
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(onDone).toHaveBeenCalledOnce();
  });

  it("stays open and renders the problem when the action fails", async () => {
    server.use(
      http.post(ARCHIVE, () =>
        HttpResponse.json(
          {
            type: "urn:dc:problem:folder-has-open-drafts",
            title: "Folder has open drafts",
            status: 409,
            detail: "Publish or discard the open drafts first.",
          },
          {
            status: 409,
            headers: {
              "Content-Type": "application/problem+json",
              "X-Request-ID": "req-77",
            },
          },
        ),
      ),
    );
    render(<Harness />);
    const { user, dialog } = await openDialog();

    await user.click(
      within(dialog).getByRole("button", { name: "Archive Payments" }),
    );

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Folder has open drafts");
    expect(alert).toHaveTextContent(
      "Publish or discard the open drafts first.",
    );
    expect(alert).toHaveTextContent("folder-has-open-drafts");
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    // The confirm is usable again for a retry.
    expect(
      within(dialog).getByRole("button", { name: "Archive Payments" }),
    ).not.toHaveAttribute("aria-busy");
  });

  it("explains a network failure in place", async () => {
    server.use(http.post(ARCHIVE, () => HttpResponse.error()));
    render(<Harness />);
    const { user, dialog } = await openDialog();

    await user.click(
      within(dialog).getByRole("button", { name: "Archive Payments" }),
    );

    expect(
      await within(dialog).findByText("Can't reach the server"),
    ).toBeInTheDocument();
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });

  it("keeps the confirm disabled until the exact name is typed", async () => {
    server.use(http.post(ARCHIVE, () => HttpResponse.json({})));
    render(<Harness typeToConfirm="Payments" />);
    const { user, dialog } = await openDialog();

    const confirm = within(dialog).getByRole("button", {
      name: "Archive Payments",
    });
    expect(confirm).toBeDisabled();
    expect(
      within(dialog).getByText("Type the name exactly to enable Archive."),
    ).toBeVisible();

    const field = within(dialog).getByLabelText("Type Payments to confirm");
    await user.type(field, "payments");
    expect(confirm).toBeDisabled();
    await user.clear(field);
    await user.type(field, "Payments ");
    expect(confirm).toBeDisabled();
    await user.clear(field);
    await user.type(field, "Payments");
    expect(confirm).toBeEnabled();

    await user.click(confirm);
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });

  it("closes on Esc and on the dismiss button and returns focus to the opener", async () => {
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "Archive folder" });
    const { user } = await openDialog();

    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    await waitFor(() => {
      expect(trigger).toHaveFocus();
    });

    await user.click(trigger);
    await user.click(
      await screen.findByRole("button", { name: "Keep Payments" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    await waitFor(() => {
      expect(trigger).toHaveFocus();
    });
  });

  it("ignores a backdrop click (static backdrop)", async () => {
    render(<Harness />);
    const { user, dialog } = await openDialog();
    const backdrop = dialog.previousElementSibling;
    if (!(backdrop instanceof HTMLElement)) throw new Error("no backdrop");
    await user.click(backdrop);
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });

  it("starts fresh after it is closed with a failure and typed text", async () => {
    server.use(http.post(ARCHIVE, () => HttpResponse.error()));
    render(<Harness typeToConfirm="Payments" />);
    const { user, dialog } = await openDialog();
    await user.type(
      within(dialog).getByLabelText("Type Payments to confirm"),
      "Payments",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Archive Payments" }),
    );
    await within(dialog).findByText("Can't reach the server");
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });

    await user.click(screen.getByRole("button", { name: "Archive folder" }));
    const again = await screen.findByRole("alertdialog");
    expect(
      within(again).getByLabelText("Type Payments to confirm"),
    ).toHaveValue("");
    expect(within(again).queryByText("Can't reach the server")).toBeNull();
  });

  it("wraps a 200-character title instead of truncating it", async () => {
    const title = `Archive ${"Payments".repeat(24)}?`;
    expect(title.length).toBeGreaterThan(200);
    render(<Harness title={title} />);
    const { dialog } = await openDialog();

    const heading = within(dialog).getByRole("heading", { name: title });
    expect(heading).toHaveTextContent(title);

    // jsdom does not lay out text, so the wrapping rule is read from the stylesheet.
    const css = readFileSync(
      resolve(import.meta.dirname, "../Dialog/Dialog.module.css"),
      "utf8",
    );
    const rule = /\.title\s*{([^}]*)}/.exec(css)?.[1] ?? "";
    expect(rule).toContain("overflow-wrap: anywhere");
    expect(rule).not.toMatch(/text-overflow|white-space:\s*nowrap/);
  });

  it("shows a generic message when the action fails with something other than an API problem", async () => {
    const user = userEvent.setup();
    render(
      <ConfirmDialog
        open
        onOpenChange={() => undefined}
        title="Delete key?"
        body="The key stops working at once."
        confirmLabel="Delete key"
        onConfirm={() => Promise.reject(new TypeError("x is undefined"))}
      />,
    );
    const dialog = await screen.findByRole("alertdialog");
    await user.click(
      within(dialog).getByRole("button", { name: "Delete key" }),
    );
    expect(
      await within(dialog).findByText(
        "The action did not complete. Try again.",
      ),
    ).toBeVisible();
    expect(within(dialog).getByText("Something went wrong")).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Go back" }),
    ).toBeEnabled();
  });

  it("refuses a submission before the name matches and a second one while the action runs", async () => {
    let finish: () => void = () => undefined;
    const onConfirm = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    const onOpenChange = vi.fn();
    const user = userEvent.setup();
    render(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="Delete Payments?"
        body="Everything below is removed."
        confirmLabel="Delete Payments"
        typeToConfirm="Payments"
        onConfirm={onConfirm}
      />,
    );
    const dialog = await screen.findByRole("alertdialog");
    const form = dialog.querySelector("form");
    if (form === null) throw new Error("no form");
    // A submission that skips the disabled button (implicit submit) still does nothing.
    fireEvent.submit(form);
    expect(onConfirm).not.toHaveBeenCalled();

    await user.type(
      within(dialog).getByLabelText("Type Payments to confirm"),
      "Payments",
    );
    fireEvent.submit(form);
    expect(
      await within(dialog).findByRole("button", { name: "Working…" }),
    ).toHaveAttribute("aria-busy", "true");
    fireEvent.submit(form);
    expect(onConfirm).toHaveBeenCalledTimes(1);

    finish();
    await waitFor(() => {
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });
  });
});
