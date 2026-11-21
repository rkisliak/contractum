// The access helpers and views: role and kind copy, the effective-access model (where each role
// was granted, the inheritance sentence, the readable path), the held-access union, the
// capability and /me hooks against msw, and every branch of the EffectiveAccess view.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRouteWithContext,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";
import { createApiClient, type ApiClient } from "@/api/client";
import type {
  EffectiveAccess as EffectiveAccessOut,
  Folder,
} from "@/api/types";
import { http, HttpResponse, server } from "@/test/msw";
import { makeFolder, makeMe, rootFolder, testConfig } from "@/test/renderApp";
import { EffectiveAccess } from "./EffectiveAccess";
import {
  folderInheritance,
  folderRoles,
  homeRoles,
  readableFolderPath,
} from "./effectiveAccessModel";
import { PERMISSION_IDS, ROLE_IDS } from "./permissionRegistry";
import { childKindOf, kindLabel, roleLabel, roleMeaning } from "./roles";
import { capabilitiesQueryKey, useCapabilities } from "./useCapabilities";
import {
  grantedGlobalPermissions,
  heldAccess,
  holdsAny,
  isBaselinePermission,
  useHeldAccess,
} from "./useHeldAccess";
import { hasGlobal, useMe } from "./useMe";

const acme = makeFolder({
  id: "acme-id",
  slug: "acme",
  display_name: "Acme",
  path: "acme",
});
const payments = makeFolder({
  id: "pay-id",
  parent_id: "acme-id",
  kind: "project",
  slug: "payments",
  display_name: "Payments",
  path: "acme/payments",
});
const ledger = makeFolder({
  id: "ledger-id",
  parent_id: "pay-id",
  kind: "team",
  slug: "ledger",
  display_name: "Ledger",
  path: "acme/payments/ledger",
});
const folders: Folder[] = [rootFolder, acme, payments, ledger];

function assignment(
  folderId: string,
  folderPath: string,
  role: string,
  viaGroup: string | null = null,
) {
  return {
    grant_id: `g-${folderId}-${role}`,
    folder_id: folderId,
    folder_path: folderPath,
    role,
    via_group_id: viaGroup,
  };
}

function access(
  overrides: Partial<EffectiveAccessOut> = {},
): EffectiveAccessOut {
  return {
    folder_id: "ledger-id",
    roles: [],
    permissions: [],
    inheritance_blocked: false,
    inheritance_blocked_at: null,
    ...overrides,
  };
}

/** Mounts `ui` under a minimal router whose root context carries the API client. */
function mount(ui: ReactNode) {
  const api: ApiClient = createApiClient(testConfig);
  const rootRoute = createRootRouteWithContext<{ api: ApiClient }>()({
    component: () => <>{ui}</>,
  });
  const router = createRouter({
    routeTree: rootRoute,
    context: { api },
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("role and kind copy", () => {
  it("labels known roles, humanises unknown ones and describes each", () => {
    for (const role of ROLE_IDS) {
      expect(roleLabel(role)).not.toContain("_");
      expect(roleMeaning(role)).toMatch(/\.$/);
    }
    expect(roleLabel("org_admin")).toBe("Organization admin");
    expect(roleLabel("data_steward_lead")).toBe("Data steward lead");
    expect(roleMeaning("data_steward_lead")).toBe("");
    // notifier is the built-in notifier account's service role, never a people role.
    expect(roleMeaning("notifier")).toBe(
      "Service role of the built-in notifier account: sends notice emails and copies webhook hosts to the allowed-hosts list once. Never grant it to people.",
    );
    expect(roleMeaning("notifier")).not.toMatch(/receives/i);
  });

  it("labels kinds and gives the child kind down to Team", () => {
    expect(kindLabel("root")).toBe("Root");
    expect(kindLabel("team")).toBe("Team");
    expect(kindLabel("galaxy")).toBe("galaxy");
    expect(childKindOf("root")).toBe("organization");
    expect(childKindOf("organization")).toBe("project");
    expect(childKindOf("project")).toBe("team");
    expect(childKindOf("team")).toBeNull();
  });
});

describe("effective access model", () => {
  it("reads a folder path by display names, Root, or the fallback", () => {
    expect(readableFolderPath("ledger-id", folders)).toBe(
      "Acme / Payments / Ledger",
    );
    expect(readableFolderPath(rootFolder.id, folders)).toBe("Root");
    expect(readableFolderPath("hidden", folders)).toBe("Root");
    expect(readableFolderPath("hidden", folders, "other/team")).toBe(
      "other/team",
    );
  });

  it("explains each held role by the grants on the folder or above", () => {
    const me = makeMe({
      groups: [
        { id: "grp-1", display_name: "Ledger editors", type: "local_group" },
      ],
      assignments: [
        assignment(rootFolder.id, "", "global_reader"),
        assignment("acme-id", "acme", "org_admin"),
        assignment("ledger-id", "acme/payments/ledger", "team_editor", "grp-1"),
        assignment("ledger-id", "acme/payments/ledger", "team_reader", "gone"),
        // A grant on an unrelated folder never explains a role here.
        assignment("other-id", "other", "org_admin"),
        // An unlisted folder whose path is an ancestor still applies.
        assignment("hidden-id", "acme/payments", "project_editor"),
      ],
    });
    const roles = folderRoles(
      me,
      ledger,
      folders,
      access({
        roles: [
          "global_reader",
          "org_admin",
          "team_editor",
          "team_reader",
          "project_editor",
          "consumer",
        ],
      }),
    );
    expect(roles).toEqual([
      {
        role: "global_reader",
        sources: [
          {
            folderId: rootFolder.id,
            label: "Root",
            here: false,
            viaGroup: null,
          },
        ],
      },
      {
        role: "org_admin",
        sources: [
          { folderId: "acme-id", label: "Acme", here: false, viaGroup: null },
        ],
      },
      {
        role: "team_editor",
        sources: [
          {
            folderId: "ledger-id",
            label: "Acme / Payments / Ledger",
            here: true,
            viaGroup: "Ledger editors",
          },
        ],
      },
      {
        role: "team_reader",
        sources: [
          {
            folderId: "ledger-id",
            label: "Acme / Payments / Ledger",
            here: true,
            viaGroup: "a group",
          },
        ],
      },
      {
        role: "project_editor",
        sources: [
          {
            folderId: "hidden-id",
            label: "acme/payments",
            here: false,
            viaGroup: null,
          },
        ],
      },
      { role: "consumer", sources: [] },
    ]);
  });

  it("drops grants above a block except a Global admin's", () => {
    const me = makeMe({
      assignments: [
        assignment(rootFolder.id, "", "global_admin"),
        assignment("acme-id", "acme", "org_admin"),
        assignment("pay-id", "acme/payments", "project_admin"),
        assignment("ledger-id", "acme/payments/ledger", "team_admin"),
      ],
    });
    const blocked = access({
      roles: ["global_admin", "org_admin", "project_admin", "team_admin"],
      inheritance_blocked: true,
      inheritance_blocked_at: "pay-id",
    });
    const sources = Object.fromEntries(
      folderRoles(me, ledger, folders, blocked).map((held) => [
        held.role,
        held.sources.map((source) => source.folderId),
      ]),
    );
    expect(sources).toEqual({
      global_admin: [rootFolder.id],
      org_admin: [],
      project_admin: ["pay-id"],
      team_admin: ["ledger-id"],
    });
    // A block whose folder is not in the chain lets only the Global admin through.
    const unknownBlock = folderRoles(
      me,
      ledger,
      folders,
      access({ ...blocked, inheritance_blocked_at: null }),
    );
    expect(unknownBlock.map((held) => held.sources.length)).toEqual([
      1, 0, 0, 0,
    ]);
    // Without /me there is nothing to explain.
    expect(
      folderRoles(undefined, ledger, folders, access({ roles: ["consumer"] })),
    ).toEqual([{ role: "consumer", sources: [] }]);
  });

  it("states the inheritance at the top, open, and blocked here, above or unknown", () => {
    expect(folderInheritance(rootFolder, folders, access())).toEqual({
      kind: "top",
    });
    expect(folderInheritance(ledger, folders, access())).toEqual({
      kind: "open",
      ancestors: [
        { id: "pay-id", label: "Acme / Payments" },
        { id: "acme-id", label: "Acme" },
        { id: rootFolder.id, label: "Root" },
      ],
    });
    expect(
      folderInheritance(
        ledger,
        folders,
        access({ inheritance_blocked: true, inheritance_blocked_at: "pay-id" }),
      ),
    ).toEqual({
      kind: "blocked",
      at: { id: "pay-id", label: "Acme / Payments" },
      here: false,
    });
    expect(
      folderInheritance(
        ledger,
        folders,
        access({
          inheritance_blocked: true,
          inheritance_blocked_at: "ledger-id",
        }),
      ),
    ).toMatchObject({ kind: "blocked", here: true });
    expect(
      folderInheritance(
        ledger,
        folders,
        access({ inheritance_blocked: true, inheritance_blocked_at: null }),
      ),
    ).toEqual({ kind: "blocked", at: null, here: false });
  });

  it("groups every assignment by role for the home card", () => {
    const me = makeMe({
      groups: [{ id: "grp-1", display_name: "Readers", type: "local_group" }],
      assignments: [
        assignment("acme-id", "acme", "org_reader"),
        assignment("ledger-id", "acme/payments/ledger", "org_reader", "grp-1"),
        assignment("x-id", "x/y", "consumer", "unknown-group"),
      ],
    });
    expect(homeRoles(me, folders)).toEqual([
      {
        role: "org_reader",
        sources: [
          { folderId: "acme-id", label: "Acme", here: false, viaGroup: null },
          {
            folderId: "ledger-id",
            label: "Acme / Payments / Ledger",
            here: false,
            viaGroup: "Readers",
          },
        ],
      },
      {
        role: "consumer",
        sources: [
          {
            folderId: "x-id",
            label: "x/y",
            here: false,
            viaGroup: "a group",
          },
        ],
      },
    ]);
  });
});

describe("held access helpers", () => {
  it("never counts baseline permissions as granted", () => {
    expect(isBaselinePermission("audit:list")).toBe(true);
    expect(isBaselinePermission("password:rotate")).toBe(true);
    expect(isBaselinePermission("user:create")).toBe(false);
    expect(isBaselinePermission("not:a-permission")).toBe(false);
    const me = makeMe({
      global_permissions: ["audit:list", "group:list", "user:read"],
    });
    expect(grantedGlobalPermissions(me)).toEqual(["user:read"]);
    const held = heldAccess(me, [["contract:read"], ["grant:manage"]]);
    expect([...held.permissions].sort()).toEqual([
      "audit:list",
      "contract:read",
      "grant:manage",
      "group:list",
      "user:read",
    ]);
    expect(holdsAny(held, "audit:list")).toBe(false);
    expect(holdsAny(held, "audit:list", "grant:manage")).toBe(true);
    expect(holdsAny(held, "folder:create")).toBe(false);
    expect(heldAccess(me).permissions.size).toBe(3);
    expect(
      heldAccess(me, [["contract:read"], ["grant:manage"]], ["a", "b"])
        .byFolder,
    ).toEqual([
      { folderId: "a", permissions: ["contract:read"] },
      { folderId: "b", permissions: ["grant:manage"] },
    ]);
    expect(hasGlobal(me, "user:read")).toBe(true);
    expect(hasGlobal(undefined, "user:read")).toBe(false);
    expect(capabilitiesQueryKey(undefined)).toEqual(["capabilities", "global"]);
    expect(capabilitiesQueryKey("f")).toEqual(["capabilities", "f"]);
  });
});

function capabilities(folderId: string | null, permissions: string[]) {
  return HttpResponse.json({
    folder_id: folderId,
    roles: [],
    permissions,
    inheritance_blocked: false,
    grantable_roles: [],
  });
}

function HeldProbe() {
  const held = useHeldAccess();
  return (
    <div>
      <p data-testid="held">
        {held.data ? [...held.data.permissions].sort().join(",") : "pending"}
      </p>
      <p data-testid="error">{held.error ? "failed" : "ok"}</p>
      <button type="button" onClick={held.refetch}>
        Refetch
      </button>
    </div>
  );
}

describe("useHeldAccess", () => {
  it("unions /me with each granted folder once, reports a failed folder and refetches", async () => {
    const actor = userEvent.setup();
    const requested: (string | null)[] = [];
    let meCalls = 0;
    server.use(
      http.get("*/api/v1/identity/me", () => {
        meCalls += 1;
        return HttpResponse.json(
          makeMe({
            global_permissions: ["audit:list"],
            assignments: [
              assignment("acme-id", "acme", "org_reader"),
              assignment("acme-id", "acme", "policy_manager"),
              assignment("ledger-id", "acme/payments/ledger", "team_editor"),
            ],
          }),
        );
      }),
      http.get("*/api/v1/identity/me/capabilities", ({ request }) => {
        const folderId = new URL(request.url).searchParams.get("folder_id");
        requested.push(folderId);
        return folderId === "ledger-id"
          ? HttpResponse.json(
              { type: "urn:dc:problem:internal", title: "Boom", status: 500 },
              {
                status: 500,
                headers: { "Content-Type": "application/problem+json" },
              },
            )
          : capabilities(folderId, ["folder:read", "rule-set:create"]);
      }),
    );
    mount(<HeldProbe />);
    await waitFor(() => {
      expect(screen.getByTestId("held")).toHaveTextContent(
        "audit:list,folder:read,rule-set:create",
      );
    });
    expect(screen.getByTestId("error")).toHaveTextContent("failed");
    // One request per distinct folder.
    expect([...requested].sort()).toEqual(["acme-id", "ledger-id"]);
    const meBefore = meCalls;
    const capsBefore = requested.length;
    await actor.click(screen.getByRole("button", { name: "Refetch" }));
    await waitFor(() => {
      expect(meCalls).toBeGreaterThan(meBefore);
      expect(requested.length).toBeGreaterThanOrEqual(capsBefore + 2);
    });
  });

  it("is ready from /me alone for a principal without assignments", async () => {
    server.use(
      http.get("*/api/v1/identity/me", () =>
        HttpResponse.json(
          makeMe({ global_permissions: ["user:read"], assignments: [] }),
        ),
      ),
    );
    mount(<HeldProbe />);
    await waitFor(() => {
      expect(screen.getByTestId("held")).toHaveTextContent("user:read");
    });
    expect(screen.getByTestId("error")).toHaveTextContent("ok");
  });

  it("treats a bodiless capability answer as a failure", async () => {
    server.use(
      http.get("*/api/v1/identity/me", () =>
        HttpResponse.json(
          makeMe({
            global_permissions: [],
            assignments: [assignment("acme-id", "acme", "org_reader")],
          }),
        ),
      ),
      http.get(
        "*/api/v1/identity/me/capabilities",
        () => new HttpResponse(null, { status: 204 }),
      ),
    );
    mount(<HeldProbe />);
    await waitFor(() => {
      expect(screen.getByTestId("error")).toHaveTextContent("failed");
    });
    expect(screen.getByTestId("held")).toHaveTextContent(/^$/);
  });
});

function MeProbe() {
  const me = useMe();
  return (
    <p data-testid="me">
      {me.isError
        ? `error: ${me.error.message}`
        : (me.data?.display_name ?? "loading")}
    </p>
  );
}

describe("useMe", () => {
  it("reads /me and fails on an empty body", async () => {
    server.use(
      http.get(
        "*/api/v1/identity/me",
        () => new HttpResponse(null, { status: 204 }),
      ),
    );
    mount(<MeProbe />);
    expect(
      await screen.findByText("error: GET /identity/me had no body"),
    ).toBeVisible();
  });
});

function CapsProbe({
  folderId,
  enabled,
}: {
  folderId?: string;
  enabled?: boolean;
}) {
  const caps = useCapabilities(
    folderId,
    enabled === undefined ? undefined : { enabled },
  );
  return (
    <p data-testid="caps">
      {caps.isPending
        ? "pending"
        : `create=${String(caps.can("folder:create"))} delete=${String(
            caps.can("contract:delete"),
          )} folder=${caps.data?.folder_id ?? "global"}`}
    </p>
  );
}

describe("useCapabilities", () => {
  it("asks globally without a folder and answers can()", async () => {
    const urls: URL[] = [];
    server.use(
      http.get("*/api/v1/identity/me/capabilities", ({ request }) => {
        const url = new URL(request.url);
        urls.push(url);
        return capabilities(url.searchParams.get("folder_id"), [
          "folder:create",
        ]);
      }),
    );
    mount(<CapsProbe />);
    expect(
      await screen.findByText("create=true delete=false folder=global"),
    ).toBeVisible();
    expect(urls).toHaveLength(1);
    expect(urls[0]?.searchParams.has("folder_id")).toBe(false);
  });

  it("asks at a folder", async () => {
    const asked: (string | null)[] = [];
    server.use(
      http.get("*/api/v1/identity/me/capabilities", ({ request }) => {
        const folderId = new URL(request.url).searchParams.get("folder_id");
        asked.push(folderId);
        return capabilities(folderId, ["contract:delete"]);
      }),
    );
    mount(<CapsProbe folderId="acme-id" />);
    expect(
      await screen.findByText("create=false delete=true folder=acme-id"),
    ).toBeVisible();
    expect(asked).toEqual(["acme-id"]);
  });

  it("never asks when disabled", async () => {
    let asked = false;
    server.use(
      http.get("*/api/v1/identity/me/capabilities", () => {
        asked = true;
        return capabilities(null, []);
      }),
    );
    mount(<CapsProbe enabled={false} />);
    expect(await screen.findByText("pending")).toBeVisible();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(asked).toBe(false);
  });

  it("holds nothing when the answer has no body", async () => {
    server.use(
      http.get(
        "*/api/v1/identity/me/capabilities",
        () => new HttpResponse(null, { status: 204 }),
      ),
    );
    mount(<CapsProbe />);
    expect(
      await screen.findByText("create=false delete=false folder=global"),
    ).toBeVisible();
  });
});

const ALL = [...PERMISSION_IDS];

describe("EffectiveAccess view", () => {
  it("shows the empty-role line and no permissions", async () => {
    const actor = userEvent.setup();
    mount(
      <EffectiveAccess
        roles={[]}
        permissions={[]}
        where="here"
        emptyRoles="Nothing granted."
      />,
    );
    expect(await screen.findByText("Nothing granted.")).toBeVisible();
    await actor.click(screen.getByText("See details"));
    expect(screen.getByText("You have no permissions here.")).toBeVisible();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText("Show technical permission names")).toBeNull();
    expect(screen.queryByText(/^No access:/)).toBeNull();
  });

  it("uses the default empty-role copy", async () => {
    mount(<EffectiveAccess roles={[]} permissions={[]} where="here" />);
    expect(await screen.findByText("You hold no role here.")).toBeVisible();
  });

  it("says full access in the role line for one role and lists every area allowed", async () => {
    const actor = userEvent.setup();
    mount(
      <EffectiveAccess
        roles={[
          {
            role: "global_admin",
            sources: [{ folderId: rootFolder.id, label: "Root", here: false }],
          },
        ]}
        permissions={ALL}
        where="everywhere"
        fullAccess="Full access to everything."
        inheritance={{ kind: "top" }}
      />,
    );
    const item = (await screen.findByText("Global admin")).closest("li");
    if (!item) throw new Error("no role item");
    expect(item).toHaveTextContent(
      "Full access to everything. Granted on Root.",
    );
    expect(within(item).getByRole("link", { name: "Root" })).toHaveAttribute(
      "href",
      `/folders/${rootFolder.id}`,
    );
    expect(
      screen.getByText(
        "This is the top of the hierarchy, so nothing is inherited here.",
      ),
    ).toBeVisible();
    await actor.click(screen.getByText("See details"));
    const table = screen.getByRole("table", {
      name: "What you can do everywhere",
    });
    expect(within(table).queryAllByText("Not allowed")).toHaveLength(0);
    expect(within(table).queryAllByText("Partly allowed")).toHaveLength(0);
    expect(within(table).getAllByText("Allowed").length).toBeGreaterThan(0);
    expect(screen.queryByText(/^No access:/)).toBeNull();
    // A fully held area has no "Not included" list.
    expect(within(table).queryByText("Not included")).toBeNull();
  });

  it("shows a folder selector for 2+ scopes and describes only the chosen one", async () => {
    const actor = userEvent.setup();
    mount(
      <EffectiveAccess
        roles={[]}
        permissions={["contract:read", "contract:create", "contract:publish"]}
        scopes={[
          {
            id: "a",
            label: "Org A",
            permissions: [
              "contract:read",
              "contract:create",
              "contract:publish",
            ],
          },
          { id: "b", label: "Org B", permissions: ["contract:read"] },
        ]}
        initialScopeId="b"
        where="somewhere"
      />,
    );
    await actor.click(await screen.findByText("See details"));
    const select = screen.getByRole("combobox", { name: "Show access at" });
    expect(select).toHaveValue("b");
    expect(
      screen.getByRole("table", { name: "What you can do at Org B and below" }),
    ).toBeVisible();
    expect(screen.getByText("contract:read")).toBeInTheDocument();
    expect(screen.queryByText("contract:publish")).toBeNull();
    await actor.selectOptions(select, "a");
    expect(
      screen.getByRole("table", { name: "What you can do at Org A and below" }),
    ).toBeVisible();
    expect(screen.getByText("contract:publish")).toBeInTheDocument();
  });

  it("shows no selector for one scope or none", async () => {
    const actor = userEvent.setup();
    mount(
      <EffectiveAccess
        roles={[]}
        permissions={["contract:read"]}
        scopes={[{ id: "a", label: "Org A", permissions: ["contract:read"] }]}
        where="here"
      />,
    );
    await actor.click(await screen.findByText("See details"));
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(
      screen.getByRole("table", { name: "What you can do here" }),
    ).toBeVisible();
  });

  it("puts full access on its own line for several roles, with the default copy", async () => {
    mount(
      <EffectiveAccess
        roles={[
          { role: "global_admin", sources: [] },
          { role: "mystery_role", sources: [] },
        ]}
        permissions={ALL}
        where="here and below"
        inheritance={{ kind: "open", ancestors: [] }}
      />,
    );
    expect(
      await screen.findByText("Full access here and below."),
    ).toBeVisible();
    const mystery = screen.getByText("Mystery role").closest("li");
    expect(mystery).toHaveTextContent(
      "A platform role. Granted on a folder above.",
    );
    expect(
      screen.getByText("Roles granted on the folders above also apply here."),
    ).toBeVisible();
  });

  it("explains sources, groups and partial access, expands a row and shows technical names", async () => {
    const actor = userEvent.setup();
    mount(
      <EffectiveAccess
        roles={[
          {
            role: "team_editor",
            sources: [
              {
                folderId: "ledger-id",
                label: "Ledger",
                here: true,
                viaGroup: "Ledger editors",
              },
              { folderId: "acme-id", label: "Acme", here: false },
            ],
          },
        ]}
        permissions={["contract:read", "folder:read", "made:up"]}
        where="here"
        inheritance={{
          kind: "open",
          ancestors: [
            { id: "pay-id", label: "Payments" },
            { id: "acme-id", label: "Acme" },
            { id: rootFolder.id, label: "Root" },
          ],
        }}
      />,
    );
    const item = (await screen.findByText("Team editor")).closest("li");
    if (!item) throw new Error("no role item");
    expect(item).toHaveTextContent(
      "Creates, edits and publishes contracts in this team. Granted on this folder through the group Ledger editors; also on Acme.",
    );
    expect(screen.getByText(/also apply here\./)).toHaveTextContent(
      "Roles granted on Payments, Acme and Root also apply here.",
    );
    await actor.click(screen.getByText("See details"));
    const table = screen.getByRole("table", { name: "What you can do here" });
    expect(within(table).getAllByText("Not allowed").length).toBeGreaterThan(0);
    expect(screen.getByText(/^No access:/)).toBeVisible();
    const row = within(table).getAllByRole("button")[0];
    if (!row) throw new Error("no area row");
    const panel = document.getElementById(
      row.getAttribute("aria-controls") ?? "",
    );
    expect(panel).not.toBeVisible();
    await actor.click(row);
    expect(row).toHaveAttribute("aria-expanded", "true");
    expect(panel).toBeVisible();
    expect(within(panel as HTMLElement).getByText("You can")).toBeVisible();
    expect(
      within(panel as HTMLElement).getByText("Not included"),
    ).toBeVisible();
    await actor.click(row);
    expect(row).toHaveAttribute("aria-expanded", "false");
    expect(panel).not.toBeVisible();

    await actor.click(screen.getByText("Show technical permission names"));
    const names = screen.getAllByRole("code").map((code) => code.textContent);
    expect(names).toEqual(["contract:read", "folder:read", "made:up"]);
    expect(screen.getByText("made:up").closest("div")?.textContent).toContain(
      "Not described yet.",
    );
  });

  it("states a block here, at an ancestor and at an unknown folder", async () => {
    const view = (
      state: Parameters<typeof EffectiveAccess>[0]["inheritance"],
    ) => (
      <EffectiveAccess
        roles={[]}
        permissions={[]}
        where="here"
        inheritance={state}
      />
    );
    mount(
      <>
        {view({ kind: "blocked", at: null, here: true })}
        {view({
          kind: "blocked",
          at: { id: "pay-id", label: "Payments" },
          here: false,
        })}
        {view({ kind: "blocked", at: null, here: false })}
      </>,
    );
    expect(
      await screen.findByText(/^Inheritance is blocked here:/),
    ).toBeVisible();
    const at = screen.getByText(/^Inheritance is blocked at/);
    expect(at).toHaveTextContent(
      "Inheritance is blocked at Payments: roles granted above it do not apply here (Global admins are exempt).",
    );
    expect(within(at).getByRole("link", { name: "Payments" })).toHaveAttribute(
      "href",
      "/folders/pay-id",
    );
    expect(
      screen.getByText(/^Inheritance is blocked above this folder/),
    ).toBeVisible();
  });
});
