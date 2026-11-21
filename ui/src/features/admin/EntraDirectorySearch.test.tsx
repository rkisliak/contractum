// The Entra directory search box on its own: the server's stripping of the typed term, the owner
// a group or service-principal search needs first, the tenant and owner sent with the query, the
// picked match's helper line, and each failure (503 with a retry, any other refusal, no body).
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import type { EntraDirectoryMatch } from "@/api/types";
import { renderWithApi } from "@/test/governance-hooks";
import { http, HttpResponse, server } from "@/test/msw";
import { EntraDirectorySearch, searchTerm } from "./EntraDirectorySearch";

const SEARCH = "*/api/v1/identity/entra/directory/search";
const TID = "11111111-2222-4333-8444-555555555555";
const OID = "0b5c3f8e-1a2b-4c3d-8e9f-0123456789ab";

function Harness({
  kind,
  ownerId = "acme-id",
  tenantId = "",
}: {
  kind: "user" | "group" | "service-principal";
  ownerId?: string;
  tenantId?: string;
}) {
  const [picked, setPicked] = useState<EntraDirectoryMatch | null>(null);
  return (
    <EntraDirectorySearch
      kind={kind}
      id="t"
      ownerId={ownerId}
      tenantId={tenantId}
      picked={picked}
      onPick={setPicked}
    />
  );
}

function problem(status: number) {
  return HttpResponse.json(
    { type: "urn:dc:problem:directory", title: "Directory", status },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

describe("searchTerm", () => {
  it("drops quotes, backslashes and control characters and folds whitespace", () => {
    expect(searchTerm('  da"ta\\ eng\tine\u0007ers  ')).toBe("data eng ineers");
    expect(searchTerm("x".repeat(120))).toHaveLength(100);
  });
});

describe("Entra directory search", () => {
  it("asks for an owner before searching groups, and requests nothing until then", async () => {
    const user = userEvent.setup();
    renderWithApi(<Harness kind="group" ownerId="" />);
    await user.type(
      await screen.findByRole("combobox", { name: "Search Entra groups" }),
      "data",
    );
    expect(await screen.findByText("Choose an owner to search.")).toBeVisible();
  });

  it("sends the tenant and owner, and shows a picked service principal's application ID", async () => {
    const user = userEvent.setup();
    const queries: URLSearchParams[] = [];
    server.use(
      http.get(SEARCH, ({ request }) => {
        queries.push(new URL(request.url).searchParams);
        return HttpResponse.json({
          items: [
            {
              object_id: OID,
              display_name: "Ingest App",
              secondary: "app-1",
              kind: "service_principal",
              tenant_id: TID,
              already_registered: false,
            },
          ],
          truncated: false,
        });
      }),
    );
    renderWithApi(<Harness kind="service-principal" tenantId={TID} />);
    await user.type(
      await screen.findByRole("combobox", {
        name: "Search Entra service principals",
      }),
      "ingest",
    );
    await user.click(await screen.findByRole("option", { name: /Ingest App/ }));
    expect(
      await screen.findByText(`Application ID app-1 · ${OID}`),
    ).toBeVisible();
    expect(queries[0]?.get("kind")).toBe("service_principal");
    expect(queries[0]?.get("tenant_id")).toBe(TID);
    expect(queries[0]?.get("owner_folder_id")).toBe("acme-id");
  });

  it("retries an unreachable directory", async () => {
    const user = userEvent.setup();
    let calls = 0;
    server.use(
      http.get(SEARCH, () => {
        calls += 1;
        return calls === 1
          ? problem(503)
          : HttpResponse.json({ items: [], truncated: false });
      }),
    );
    renderWithApi(<Harness kind="user" />);
    await user.type(
      await screen.findByRole("combobox", { name: "Search Entra users" }),
      "erin",
    );
    await user.click(
      await screen.findByRole("button", { name: "Retry search" }),
    );
    await waitFor(() => {
      expect(calls).toBe(2);
    });
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Retry search" })).toBeNull();
    });
  });

  it("says the search failed for any other refusal", async () => {
    const user = userEvent.setup();
    server.use(http.get(SEARCH, () => problem(500)));
    renderWithApi(<Harness kind="user" />);
    await user.type(
      await screen.findByRole("combobox", { name: "Search Entra users" }),
      "erin",
    );
    expect(
      await screen.findByText(
        "The directory search failed. Retry, or enter an object ID instead.",
      ),
    ).toBeVisible();
  });

  it("reads an answer without a body as no matches", async () => {
    const user = userEvent.setup();
    server.use(http.get(SEARCH, () => new HttpResponse(null, { status: 204 })));
    renderWithApi(<Harness kind="user" />);
    const box = await screen.findByRole("combobox", {
      name: "Search Entra users",
    });
    await user.type(box, "erin");
    expect(
      await screen.findByText(/No matches in the directory/),
    ).toBeInTheDocument();
  });
});
