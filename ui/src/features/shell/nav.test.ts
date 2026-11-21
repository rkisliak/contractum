import { describe, expect, it } from "vitest";
import { roleLabel, childKindOf } from "@/features/access/roles";
import {
  PERMISSION_IDS,
  PERMISSION_TARGETS,
} from "@/features/access/permissionRegistry";
import { heldAccess } from "@/features/access/useHeldAccess";
import { makeMe } from "@/test/renderApp";
import { isCurrent, visibleSections } from "./nav";

function entryIds(sections: ReturnType<typeof visibleSections>): string[] {
  return sections.flatMap((section) =>
    section.entries.map((entry) => entry.id),
  );
}

describe("navigation registry", () => {
  it("shows every area to a global admin", () => {
    const ids = entryIds(
      visibleSections(
        heldAccess(makeMe({ global_permissions: [...PERMISSION_IDS] })),
      ),
    );
    expect(ids).toEqual([
      "home",
      "folders",
      "contracts",
      "templates",
      "rule-sets",
      "dry-run",
      "drift",
      "cards",
      "audit",
      "users",
      "groups",
      "service-accounts",
      "pats",
      "settings",
    ]);
  });

  // The Team editor on the live demo: baseline principal permissions globally (every signed-in
  // principal holds them) plus its capabilities on the Ledger team.
  const baseline = PERMISSION_IDS.filter(
    (permission) => PERMISSION_TARGETS[permission] === "principal",
  );
  const editor = makeMe({
    global_permissions: [...baseline],
    assignments: [
      {
        grant_id: "g",
        folder_id: "ledger",
        folder_path: "acme/payments/ledger",
        role: "team_editor",
        via_group_id: "group",
      },
    ],
  });
  const editorCaps = [
    "contract:create",
    "contract:delete",
    "contract:publish",
    "contract:read",
    "contract:read-metadata",
    "contract:read-published",
    "contract:update",
    "contract:validate",
    "contract:view",
    "folder:read",
    "ruleset:read-published",
  ];

  it("never shows an area for the baseline permissions every principal holds", () => {
    const ids = entryIds(visibleSections(heldAccess(editor, [editorCaps])));
    expect(ids).toEqual(["home", "folders", "contracts", "rule-sets", "cards"]);
    expect(
      visibleSections(heldAccess(editor, [editorCaps])).map((s) => s.id),
    ).not.toContain("admin");
    // Without any folder capability only Home, Folders and Subscriptions remain.
    expect(entryIds(visibleSections(heldAccess(editor)))).toEqual([
      "home",
      "folders",
      "cards",
    ]);
  });

  it("shows an organization admin the admin screens its capabilities allow", () => {
    const ids = entryIds(
      visibleSections(
        heldAccess(editor, [
          [
            "group:read",
            "service-account:read",
            "audit:query",
            "template:read",
          ],
        ]),
      ),
    );
    expect(ids).toEqual(
      expect.arrayContaining([
        "templates",
        "audit",
        "groups",
        "service-accounts",
      ]),
    );
    expect(ids).not.toContain("users");
  });

  it("shows an Entra administrator Groups and Service accounts, where Entra registrations now live", () => {
    // entra:manage held at an Organization: no group, service-account or user permission at all.
    const ids = entryIds(
      visibleSections(heldAccess(editor, [["entra:manage"]])),
    );
    expect(ids).toEqual(expect.arrayContaining(["groups", "service-accounts"]));
    expect(ids).not.toContain("users");
    expect(ids).not.toContain("pats");
  });

  it("lists the Admin section as Users, Groups, Service accounts, Access tokens, Settings", () => {
    const admin = visibleSections(
      heldAccess(makeMe({ global_permissions: [...PERMISSION_IDS] })),
    ).find((section) => section.id === "admin");
    expect(admin?.entries.map((entry) => entry.label)).toEqual([
      "Users",
      "Groups",
      "Service accounts",
      "Access tokens",
      "Settings",
    ]);
  });

  it("shows Settings only to a holder of settings:read", () => {
    const reader = makeMe({
      global_permissions: PERMISSION_IDS.filter((id) => id !== "settings:read"),
    });
    const withIt = entryIds(
      visibleSections(
        heldAccess(makeMe({ global_permissions: ["settings:read"] })),
      ),
    );
    expect(withIt).toContain("settings");
    expect(entryIds(visibleSections(heldAccess(reader)))).not.toContain(
      "settings",
    );
  });

  it("shows only always-visible entries before access is known", () => {
    expect(entryIds(visibleSections(undefined))).toEqual(["home", "folders"]);
  });

  it("marks the current entry by path prefix, and Home only exactly", () => {
    expect(isCurrent({ to: "/" }, "/")).toBe(true);
    expect(isCurrent({ to: "/" }, "/folders")).toBe(false);
    expect(isCurrent({ to: "/folders" }, "/folders/abc/contracts")).toBe(true);
    expect(isCurrent({ to: "/folders" }, "/foldersx")).toBe(false);
  });
});

describe("role and kind labels", () => {
  it("reads API role ids in sentence case", () => {
    expect(roleLabel("org_admin")).toBe("Organization admin");
    expect(roleLabel("brand_new_role")).toBe("Brand new role");
  });

  it("follows the fixed hierarchy", () => {
    expect(childKindOf("root")).toBe("organization");
    expect(childKindOf("project")).toBe("team");
    expect(childKindOf("team")).toBeNull();
  });
});
