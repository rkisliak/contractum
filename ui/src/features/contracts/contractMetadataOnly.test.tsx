// The contract page for callers who may see metadata only (a Policy Manager without a contract
// role, D-12, TREE-03, Pitfall 4): an info notice instead of an error, no tab or link that needs
// content or published reads, and no request the caller's role refuses. Team readers and consumers
// keep what they had.
import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { tokenStore } from "@/auth/tokenStore";
import {
  CONTRACT_ALL,
  contractFolders,
  contractHandlers,
  makeContract,
} from "@/test/contracts-fixtures";
import { server } from "@/test/msw";
import { renderApp } from "@/test/renderApp";

afterEach(() => {
  tokenStore.clear();
  server.events.removeAllListeners();
});

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

function open(permissions: string[], redacted: boolean) {
  const requested: string[] = [];
  server.events.on("request:start", ({ request }) => {
    requested.push(new URL(request.url).pathname);
  });
  renderApp("/contracts/c-1", {
    folders: contractFolders,
    permissions,
    handlers: contractHandlers({
      contract: makeContract({ content_redacted: redacted }),
    }),
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
