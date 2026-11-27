// The contract page for callers who may see metadata only (a Policy Manager without a contract
// role, D-12, TREE-03, Pitfall 4): an info notice instead of an error, no tab or link that needs
// content or published reads, and no request the caller's role refuses. Team readers and consumers
// keep what they had.
import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import {
  CONTRACT_ALL,
  CONTRACT_API,
  contractFolders,
  contractHandlers,
  makeContract,
} from "@/test/contracts-fixtures";
import { http, HttpResponse, server } from "@/test/msw";
import { renderApp } from "@/test/renderApp";
import { contractAccess } from "./ContractLayout";

afterEach(() => {
  tokenStore.clear();
  server.events.removeAllListeners();
});

const REDACTED_FACT = "Not available at your access level";
const NOTICE =
  "You can see this contract's metadata only. Its content and versions are not available at your access level.";

const POLICY_MANAGER = ["contract:read-metadata", "contract:view"];
const TEAM_READER = [
  "contract:read",
  "contract:read-metadata",
  "contract:read-published",
  "contract:view",
];
const CONSUMER = ["contract:read-published", "contract:view"];

// The API nulls the content-derived fields of a metadata-only read (they are redacted, not empty).
const REDACTED_FIELDS = { domain: null, tags: null, api_version: null };

function open(
  permissions: string[],
  redacted: boolean,
  path = "/contracts/c-1",
) {
  const requested: string[] = [];
  server.events.on("request:start", ({ request }) => {
    requested.push(new URL(request.url).pathname);
  });
  renderApp(path, {
    folders: contractFolders,
    permissions,
    handlers: [
      // The Rule sets tab answers a metadata read, so a metadata-only caller may open it.
      http.get(`${CONTRACT_API}/rule-sets`, () =>
        HttpResponse.json({ items: [], attachments_revision: 1 }),
      ),
      http.get(
        "*/api/v1/validation/folders/ledger-id/available-rule-sets",
        () => HttpResponse.json({ items: [] }),
      ),
      ...contractHandlers({
        contract: makeContract(
          redacted
            ? { content_redacted: true, ...REDACTED_FIELDS }
            : { content_redacted: false },
        ),
      }),
    ],
  });
  return requested;
}

async function tabNames() {
  const nav = await screen.findByRole("navigation", {
    name: "Contract sections",
  });
  await screen.findByRole("heading", { name: "Orders" });
  return Array.from(nav.querySelectorAll("a")).map((a) => a.textContent);
}

describe("a metadata-only caller (Policy Manager)", () => {
  it("sees the info notice, not an error", async () => {
    open(POLICY_MANAGER, true);
    const notice = await screen.findByText(NOTICE);
    expect(notice.closest("[role=status]")).not.toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/not found/i)).toBeNull();
  });

  it("keeps Overview and Rule sets and drops every tab that needs content", async () => {
    open(POLICY_MANAGER, true);
    await screen.findByText(NOTICE);
    const names = await tabNames();
    expect(names).toEqual(["Overview", "Rule sets"]);
  });

  it("shows the Latest version as plain text and offers no versions link", async () => {
    open(POLICY_MANAGER, true);
    await screen.findByText(NOTICE);
    expect(screen.getByText("1.0.0", { selector: "dd" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "1.0.0" })).toBeNull();
    expect(screen.queryByRole("link", { name: "View versions" })).toBeNull();
  });

  it("says redacted fields are not available at the access level, never that they are not set", async () => {
    open(POLICY_MANAGER, true);
    await screen.findByText(NOTICE);
    const cellOf = (label: string) =>
      screen.getByText(label, { selector: "dt" }).nextElementSibling;
    for (const label of ["Domain", "Tags", "ODCS apiVersion"]) {
      expect(cellOf(label)?.textContent).toBe(REDACTED_FACT);
    }
    // Fields the caller does read keep their own wording.
    expect(cellOf("ODCS status")?.textContent).toBe("active");
    expect(screen.queryByText("Not set")).toBeNull();
  });

  it("sends no request the role refuses", async () => {
    const requested = open(POLICY_MANAGER, true);
    await screen.findByText(NOTICE);
    await tabNames();
    await waitFor(() => {
      expect(requested.some((path) => path.endsWith("/contracts/c-1"))).toBe(
        true,
      );
    });
    const refused = requested.filter((path) =>
      /\/(versions|document|diff|drafts)(\/|$)/.test(path),
    );
    expect(refused).toEqual([]);
  });
});

describe("a metadata-only caller who opens a content route directly", () => {
  const CONTENT_ROUTES = [
    "versions",
    "document",
    "diff",
    "drafts",
    "subscribers",
  ];

  it.each(CONTENT_ROUTES)(
    "sees the notice on /%s and no request for it is sent",
    async (route) => {
      const requested = open(POLICY_MANAGER, true, `/contracts/c-1/${route}`);
      await screen.findByText(NOTICE);
      await tabNames();
      await waitFor(() => {
        expect(requested.some((path) => path.endsWith("/contracts/c-1"))).toBe(
          true,
        );
      });
      expect(screen.queryByRole("alert")).toBeNull();
      const refused = requested.filter((path) =>
        new RegExp(`/c-1/${route}(/|$)`).test(path),
      );
      expect(refused).toEqual([]);
    },
  );

  it("still opens Rule sets", async () => {
    open(POLICY_MANAGER, true, "/contracts/c-1/rule-sets");
    await tabNames();
    expect(await screen.findByText("No attached rule sets")).toBeVisible();
    expect(screen.queryByText(NOTICE)).toBeNull();
  });
});

describe("a metadata-only caller who opens the Overview with another spelling of the id", () => {
  it("sees the Overview, not only the notice, for an upper-case id in the URL", async () => {
    renderApp("/contracts/C-1", {
      folders: contractFolders,
      permissions: POLICY_MANAGER,
      handlers: [
        // The API answers any spelling of the id with the canonical (lower-case) one.
        http.get("*/api/v1/contracts/contracts/:id", () =>
          HttpResponse.json(
            makeContract({ content_redacted: true, ...REDACTED_FIELDS }),
          ),
        ),
      ],
    });
    await screen.findByText(NOTICE);
    // The Overview's facts are on the page, not just the notice.
    expect(
      await screen.findByText("ODCS id", { selector: "dt" }),
    ).toBeVisible();
  });

  it("still keeps an upper-case id off a content route", async () => {
    const requested: string[] = [];
    server.events.on("request:start", ({ request }) => {
      requested.push(new URL(request.url).pathname);
    });
    renderApp("/contracts/C-1/versions", {
      folders: contractFolders,
      permissions: POLICY_MANAGER,
      handlers: [
        http.get("*/api/v1/contracts/contracts/:id", () =>
          HttpResponse.json(
            makeContract({ content_redacted: true, ...REDACTED_FIELDS }),
          ),
        ),
      ],
    });
    await screen.findByText(NOTICE);
    await waitFor(() => {
      expect(requested.some((path) => /\/contracts\/c-1$/i.test(path))).toBe(
        true,
      );
    });
    expect(screen.queryByText("ODCS id", { selector: "dt" })).toBeNull();
    expect(requested.filter((path) => /\/versions(\/|$)/.test(path))).toEqual(
      [],
    );
  });
});

describe("contractAccess while the capabilities load", () => {
  it("offers a metadata-only caller no content tab (no flash of Drafts or Subscribers)", () => {
    const access = contractAccess(
      { content_redacted: true },
      { isPending: true, can: () => false },
    );
    expect(access.canRead).toBe(false);
    expect(access.canSeeVersions).toBe(false);
  });

  it("still offers a full reader the content tabs while loading", () => {
    const access = contractAccess(
      { content_redacted: false },
      { isPending: true, can: () => false },
    );
    expect(access.canRead).toBe(true);
    expect(access.canSeeVersions).toBe(true);
  });
});

describe("a team reader (regression)", () => {
  it("keeps every tab and the Latest link, with no notice", async () => {
    open(TEAM_READER, false);
    const names = await tabNames();
    expect(names).toEqual([
      "Overview",
      "Document",
      "Drafts",
      "Versions",
      "Diff",
      "Rule sets",
      "Subscribers",
    ]);
    expect(screen.queryByText(NOTICE)).toBeNull();
    expect(screen.queryByText(REDACTED_FACT)).toBeNull();
    expect(screen.getByText("payments", { selector: "dd" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "1.0.0" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "View versions" })).toBeTruthy();
  });

  it("holds when the caller has every contract right", async () => {
    open(CONTRACT_ALL, false);
    const names = await tabNames();
    expect(names).toContain("Versions");
    expect(names).toContain("Subscribers");
  });
});

describe("a consumer (regression)", () => {
  it("keeps Document, Versions and Diff", async () => {
    open(CONSUMER, false);
    const names = await tabNames();
    expect(names).toEqual(["Overview", "Document", "Versions", "Diff"]);
  });
});
