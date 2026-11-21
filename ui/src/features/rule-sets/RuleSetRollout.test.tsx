// The Rollout tab as a component: every mode a version can show, the mandatory switch in both
// directions, the enforce-date cells, and the guards of the pause and reschedule dialogs.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { describe, expect, it } from "vitest";
import type { ResolvedEntry, RuleSetVersionSummary } from "@/api/types";
import { renderWithApi } from "@/test/governance-hooks";
import { http, HttpResponse, server } from "@/test/msw";
import { RuleSetRollout } from "./RuleSetRollout";

const V = "*/api/v1/validation";

function version(
  n: number,
  overrides: Partial<RuleSetVersionSummary> = {},
): RuleSetVersionSummary {
  return {
    version_no: n,
    content_sha256: `sha-${String(n)}`,
    rule_keys: [],
    deprecated_at: null,
    enforce_at: null,
    published_at: "2026-11-10T10:00:00Z",
    published_by: "local:admin",
    ...overrides,
  };
}

function entry(
  n: number,
  overrides: Partial<ResolvedEntry> = {},
): ResolvedEntry {
  return {
    version_no: n,
    mode: "warn",
    enforce_at: null,
    paused: false,
    ...overrides,
  };
}

function mount(props: Partial<ComponentProps<typeof RuleSetRollout>> = {}) {
  renderWithApi(
    <RuleSetRollout
      ruleSetId="rs-1"
      name="acme-baseline"
      folderLabel="acme"
      mandatory={false}
      versions={[]}
      resolved={[]}
      canMandate={false}
      canEnforce={false}
      {...props}
    />,
  );
}

function rowOf(label: string): HTMLElement {
  return screen
    .getByRole("rowheader", { name: label })
    .closest("tr") as HTMLElement;
}

describe("rule set rollout tab", () => {
  it("shows each version's mode and date, newest first", async () => {
    mount({
      mandatory: true,
      versions: [
        version(1, { deprecated_at: "2026-11-11T00:00:00Z" }),
        version(2),
        version(3, { enforce_at: "2026-12-01T00:00:00Z" }),
        version(4),
        version(5),
      ],
      resolved: [
        entry(3, { paused: true }),
        entry(4, { mode: "enforce", enforce_at: "2026-11-12T00:00:00Z" }),
        entry(5),
      ],
    });
    expect(
      await screen.findByText("Mandatory for acme and everything below."),
    ).toBeVisible();
    expect(
      screen.getAllByRole("rowheader").map((cell) => cell.textContent),
    ).toEqual([
      "Version 5",
      "Version 4",
      "Version 3",
      "Version 2",
      "Version 1",
    ]);
    expect(within(rowOf("Version 1")).getByText("Deprecated")).toBeVisible();
    expect(within(rowOf("Version 2")).getByText("Not applied")).toBeVisible();
    expect(within(rowOf("Version 2")).getByText("Not scheduled")).toBeVisible();
    expect(within(rowOf("Version 3")).getByText("Paused")).toBeVisible();
    expect(
      within(rowOf("Version 3")).getByText("1 Dec 2026 UTC once resumed"),
    ).toBeVisible();
    expect(within(rowOf("Version 4")).getByText("Enforced")).toBeVisible();
    expect(
      within(rowOf("Version 4")).getByText(/^12 Nov 2026 UTC \(/),
    ).toBeVisible();
    expect(within(rowOf("Version 5")).getByText("Warning")).toBeVisible();
    // No action is offered without the rights.
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("makes a set mandatory and stops requiring it through the confirmations", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    server.use(
      http.put(`${V}/rule-sets/rs-1/mandatory`, async ({ request }) => {
        bodies.push(await request.json());
        return new HttpResponse(null, { status: 204 });
      }),
    );
    mount({ canMandate: true });
    expect(await screen.findByText("Nothing published yet")).toBeVisible();
    await user.click(
      screen.getByRole("checkbox", {
        name: "Mandatory for acme and everything below",
      }),
    );
    let dialog = await screen.findByRole("alertdialog", {
      name: "Make acme-baseline mandatory?",
    });
    await user.click(within(dialog).getByRole("button", { name: "Go back" }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    await user.click(
      screen.getByRole("checkbox", {
        name: "Mandatory for acme and everything below",
      }),
    );
    dialog = await screen.findByRole("alertdialog", {
      name: "Make acme-baseline mandatory?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Make mandatory" }),
    );
    await waitFor(() => {
      expect(bodies).toEqual([{ mandatory: true }]);
    });
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });

  it("stops requiring a mandatory set", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    server.use(
      http.put(`${V}/rule-sets/rs-1/mandatory`, async ({ request }) => {
        bodies.push(await request.json());
        return new HttpResponse(null, { status: 204 });
      }),
    );
    mount({ canMandate: true, mandatory: true });
    await user.click(
      await screen.findByRole("checkbox", {
        name: "Mandatory for acme and everything below",
      }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Stop requiring acme-baseline?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Stop requiring" }),
    );
    await waitFor(() => {
      expect(bodies).toEqual([{ mandatory: false }]);
    });
  });

  it("says a set that is not mandatory applies only where attached", async () => {
    mount();
    expect(
      await screen.findByText(
        "Not mandatory: it applies only where a contract attaches it.",
      ),
    ).toBeVisible();
  });

  it("starts the date picker empty for a version without a date and requires one", async () => {
    const user = userEvent.setup();
    mount({
      canReschedule: true,
      versions: [version(1)],
      resolved: [entry(1)],
    });
    await user.click(
      await screen.findByRole("button", { name: "Change date… version 1" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Change the enforcement date of version 1?",
    });
    const field = within(dialog).getByLabelText("Blocks publishes from (UTC)");
    expect(field).toHaveValue("");
    await user.click(within(dialog).getByRole("button", { name: "Save date" }));
    expect(await within(dialog).findByText("Choose a date.")).toBeVisible();
  });

  it("offers Enforce now alone to a caller who may not change dates", async () => {
    mount({ canEnforce: true, versions: [version(1)], resolved: [entry(1)] });
    expect(
      await screen.findByRole("button", { name: "Enforce version 1 now" }),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: /Change date/ })).toBeNull();
  });

  it("closes the pause confirmation without a request when no ROOT route is known", async () => {
    const user = userEvent.setup();
    mount({ canPause: true, versions: [version(1)], resolved: [entry(1)] });
    await user.click(await screen.findByRole("button", { name: "Pause" }));
    const dialog = await screen.findByRole("alertdialog", {
      name: "Pause acme-baseline?",
    });
    // msw fails the test on any unmocked request, so the guard sending nothing is proven here.
    await user.click(
      within(dialog).getByRole("button", { name: "Pause acme-baseline" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });
});
