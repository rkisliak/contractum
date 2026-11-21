import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Checkbox } from "@/components/form";
import { SecretRevealDialog } from "@/components/SecretRevealDialog";
import type { SecretKind } from "@/components/SecretRevealDialog";

const SECRET =
  "dcp_ab12cd34_Zx9-very-secret-value-that-must-never-linger_0123456789";

// Consumer-shaped harness (Phase 12): a "Create token" action produces the secret, the dialog
// shows it once, and onClosed clears the caller's copy. A QueryClient proves nothing is cached.
function Harness({
  kind = "token",
  onClosed,
  queryClient,
}: {
  kind?: SecretKind;
  onClosed?: () => void;
  queryClient?: QueryClient;
}) {
  const [secret, setSecret] = useState<string | null>(null);
  return (
    <>
      <button
        type="button"
        onClick={() => {
          setSecret(SECRET);
        }}
      >
        Create {kind}
      </button>
      <SecretRevealDialog
        open={secret !== null}
        kind={kind}
        secret={secret ?? ""}
        onClosed={() => {
          setSecret(null);
          queryClient?.clear();
          onClosed?.();
        }}
      />
    </>
  );
}

function stubClipboard(writeText: (text: string) => Promise<void>) {
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
}

async function openDialog(kind: SecretKind = "token") {
  const user = userEvent.setup();
  // userEvent.setup() replaces navigator.clipboard, so tests stub it after this call.
  const trigger = screen.getByRole("button", { name: `Create ${kind}` });
  await user.click(trigger);
  const dialog = await screen.findByRole("dialog");
  return { user, dialog, trigger };
}

describe("SecretRevealDialog", () => {
  beforeEach(() => {
    stubClipboard(() => Promise.resolve());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("cannot be closed by Esc, a backdrop click or a close button", async () => {
    render(<Harness />);
    const { user, dialog } = await openDialog();

    expect(
      within(dialog).queryByRole("button", { name: /close dialog/i }),
    ).toBeNull();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    const backdrop = dialog.previousElementSibling;
    if (!(backdrop instanceof HTMLElement)) throw new Error("no backdrop");
    await user.click(backdrop);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(within(dialog).getByRole("textbox")).toHaveValue(SECRET);
  });

  it("shows the warning and a read-only monospace field that selects on focus", async () => {
    render(<Harness />);
    const { user, dialog } = await openDialog();

    expect(
      within(dialog).getByRole("heading", { name: "Your new token" }),
    ).toBeInTheDocument();
    expect(dialog).toHaveAccessibleDescription(
      /This is the only time this token is shown\. Store it in your secret manager now\. Copying does not clear your clipboard automatically\./,
    );
    const field = within(dialog).getByRole("textbox", { name: "Token" });
    expect(field).toHaveAttribute("readonly");
    expect(field).toHaveValue(SECRET);

    // Selection survives a move away and back: focus selects the whole value.
    (field as HTMLTextAreaElement).setSelectionRange(0, 0);
    await user.tab();
    (field as HTMLTextAreaElement).focus();
    expect((field as HTMLTextAreaElement).selectionStart).toBe(0);
    expect((field as HTMLTextAreaElement).selectionEnd).toBe(SECRET.length);

    // The monospace face and wrapping live in the stylesheet (jsdom does no layout).
    const css = readFileSync(
      resolve(import.meta.dirname, "SecretRevealDialog.module.css"),
      "utf8",
    );
    const rule = /\.field\s*{([^}]*)}/.exec(css)?.[1] ?? "";
    expect(rule).toContain("font-family: var(--font-family-monospace)");
    expect(rule).toContain("overflow-wrap: anywhere");
  });

  it("keeps Close disabled until the box is ticked, then closes and removes the secret", async () => {
    const onClosed = vi.fn();
    const queryClient = new QueryClient();
    queryClient.setQueryData(["tokens"], [{ id: "t-1" }]);
    const { container } = render(
      <Harness onClosed={onClosed} queryClient={queryClient} />,
    );
    const { user, dialog } = await openDialog();

    const close = within(dialog).getByRole("button", {
      name: "Close after storing token",
    });
    expect(close).toBeDisabled();
    expect(
      within(dialog).getByText("Tick the box to enable closing."),
    ).toBeVisible();
    expect(close).toHaveAccessibleDescription(
      "Tick the box to enable closing.",
    );

    await user.click(
      within(dialog).getByRole("checkbox", {
        name: "I have stored this token",
      }),
    );
    expect(close).toBeEnabled();
    expect(
      within(dialog).queryByText("Tick the box to enable closing."),
    ).toBeNull();

    await user.click(close);
    expect(onClosed).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.body).not.toHaveTextContent(SECRET);
    expect(container.ownerDocument.body.innerHTML).not.toContain(SECRET);
    expect(document.querySelector("textarea")).toBeNull();
    // Nothing reached a cache or storage, and a later reopen starts unticked.
    expect(JSON.stringify(queryClient.getQueryCache().getAll())).not.toContain(
      SECRET,
    );
    expect(
      JSON.stringify([
        Object.entries(localStorage),
        Object.entries(sessionStorage),
      ]),
    ).not.toContain(SECRET);
    expect(window.location.href).not.toContain(SECRET);
  });

  it("removes the secret on close even when the caller keeps it open", async () => {
    const onClosed = vi.fn();
    const user = userEvent.setup();
    render(
      <SecretRevealDialog
        open
        kind="token"
        secret={SECRET}
        onClosed={onClosed}
      />,
    );
    const dialog = await screen.findByRole("dialog");
    await user.click(
      within(dialog).getByRole("checkbox", {
        name: "I have stored this token",
      }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Close after storing token" }),
    );
    expect(onClosed).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.body).not.toHaveTextContent(SECRET);
  });

  it("starts unticked when it is opened again", async () => {
    render(<Harness />);
    const { user, dialog, trigger } = await openDialog();
    await user.click(
      within(dialog).getByRole("checkbox", {
        name: "I have stored this token",
      }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Close after storing token" }),
    );
    await user.click(trigger);
    const again = await screen.findByRole("dialog");
    expect(
      within(again).getByRole("checkbox", { name: "I have stored this token" }),
    ).not.toBeChecked();
    expect(
      within(again).getByRole("button", { name: "Close after storing token" }),
    ).toBeDisabled();
  });

  it("returns focus to the control that opened it after closing", async () => {
    render(<Harness />);
    const { user, dialog, trigger } = await openDialog();
    await user.click(
      within(dialog).getByRole("checkbox", {
        name: "I have stored this token",
      }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Close after storing token" }),
    );
    await waitFor(() => {
      expect(trigger).toHaveFocus();
    });
  });

  it("copies the secret and announces it without putting it in the live region", async () => {
    const writeText = vi
      .fn<(text: string) => Promise<void>>()
      .mockResolvedValue();
    render(<Harness />);
    const { user, dialog } = await openDialog();
    stubClipboard(writeText);

    await user.click(
      within(dialog).getByRole("button", { name: "Copy token" }),
    );

    expect(writeText).toHaveBeenCalledWith(SECRET);
    expect(
      await within(dialog).findByRole("button", { name: "Token copied" }),
    ).toBeInTheDocument();
    const region = within(dialog).getByText("Token copied to clipboard");
    expect(region).toHaveAttribute("role", "status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).not.toHaveTextContent(SECRET);
    expect(within(dialog).queryByRole("alert")).toBeNull();
  });

  it("backstop: a rejected clipboard shows the danger alert and selects the field", async () => {
    const writeText = vi
      .fn<(text: string) => Promise<void>>()
      .mockRejectedValue(new DOMException("denied", "NotAllowedError"));
    render(<Harness />);
    const { user, dialog } = await openDialog();
    stubClipboard(writeText);
    const field = within(dialog).getByRole<HTMLTextAreaElement>("textbox", {
      name: "Token",
    });
    field.setSelectionRange(0, 0);

    await user.click(
      within(dialog).getByRole("button", { name: "Copy token" }),
    );

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(
      "Copy failed. Select the token and copy it manually.",
    );
    expect(field).toHaveFocus();
    expect(field.selectionStart).toBe(0);
    expect(field.selectionEnd).toBe(SECRET.length);
    // The button never claimed success.
    expect(
      within(dialog).getByRole("button", { name: "Copy token" }),
    ).toBeInTheDocument();
  });

  it.each<[SecretKind, string]>([
    ["key", "Key"],
    ["webhook secret", "Webhook secret"],
  ])("uses the %s wording throughout", async (kind, capitalised) => {
    render(<Harness kind={kind} />);
    const { dialog } = await openDialog(kind);

    expect(dialog).toHaveAccessibleDescription(
      new RegExp(`only time this ${kind} is shown`),
    );
    expect(
      within(dialog).getByRole("textbox", { name: capitalised }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: `Copy ${kind}` }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("checkbox", {
        name: `I have stored this ${kind}`,
      }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", {
        name: `Close after storing ${kind}`,
      }),
    ).toBeDisabled();
  });
});

describe("Checkbox", () => {
  it("is a native checkbox whose label toggles it", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <Checkbox
        id="remember"
        label="Remember this choice"
        onChange={onChange}
        helper="Applies to this browser."
      />,
    );
    const box = screen.getByRole("checkbox", { name: "Remember this choice" });
    expect(box).toHaveAccessibleDescription("Applies to this browser.");
    expect(box).not.toBeChecked();
    await user.click(screen.getByText("Remember this choice"));
    expect(box).toBeChecked();
    expect(onChange).toHaveBeenCalledOnce();
    box.focus();
    await user.keyboard(" ");
    expect(box).not.toBeChecked();
  });

  it("marks an error with aria-invalid and a message that names the fix", () => {
    render(
      <Checkbox
        id="terms"
        label="I accept the policy"
        error="Tick the box to continue."
      />,
    );
    const box = screen.getByRole("checkbox", { name: "I accept the policy" });
    expect(box).toHaveAttribute("aria-invalid", "true");
    expect(box).toHaveAccessibleDescription("Tick the box to continue.");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Tick the box to continue.",
    );
  });

  it("is inert when disabled", async () => {
    const user = userEvent.setup();
    render(<Checkbox id="locked" label="Locked option" disabled />);
    const box = screen.getByRole("checkbox", { name: "Locked option" });
    expect(box).toBeDisabled();
    await user.click(screen.getByText("Locked option"));
    expect(box).not.toBeChecked();
  });
});
