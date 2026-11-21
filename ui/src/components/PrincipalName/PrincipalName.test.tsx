// PrincipalName: ids rendered in the same tick are named by one POST /principals/names (at most
// 100 ids per call), the short id shows until a name arrives and stays for an id identity does not
// name. Mounted under a minimal router whose root context carries the API client, as in the app.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRouteWithContext,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";
import { createApiClient, type ApiClient } from "@/api/client";
import { http, HttpResponse, server } from "@/test/msw";
import { testConfig } from "@/test/renderApp";
import { PrincipalName, shortPrincipal, usePrincipalLabel } from "./index";

const NAMES = "*/api/v1/identity/principals/names";
const ADA = "local:3f2a1b9c-0000-4000-8000-00000000000a";
const BOB = "local:7c1d2e3f-0000-4000-8000-00000000000b";
const GONE = "entra:9e9e9e9e-0000-4000-8000-00000000000c";

/** Answers name lookups from `known` and records every requested id list. */
function nameService(known: Record<string, string>): string[][] {
  const calls: string[][] = [];
  server.use(
    http.post(NAMES, async ({ request }) => {
      const { ids } = (await request.json()) as { ids: string[] };
      calls.push(ids);
      return HttpResponse.json({
        items: ids
          .filter((id) => id in known)
          .map((id) => ({ id, display_name: known[id] ?? "" })),
      });
    }),
  );
  return calls;
}

function renderWithApi(ui: () => ReactNode) {
  const api: ApiClient = createApiClient(testConfig);
  const queryClient = new QueryClient();
  const rootRoute = createRootRouteWithContext<{ api: ApiClient }>()({
    component: () => (
      <QueryClientProvider client={queryClient}>
        <Outlet />
      </QueryClientProvider>
    ),
  });
  const page = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: () => <>{ui()}</>,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([page]),
    context: { api },
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  render(<RouterProvider router={router} />);
}

describe("shortPrincipal", () => {
  it("keeps the namespace and the first 8 characters of the id", () => {
    expect(shortPrincipal(ADA)).toBe("local:3f2a1b9c");
    expect(shortPrincipal("svc:ci:0123456789abcdef")).toBe("svc:01234567");
  });

  it("keeps an id without a namespace and says Not set for none", () => {
    expect(shortPrincipal("system")).toBe("system");
    expect(shortPrincipal(null)).toBe("Not set");
    expect(shortPrincipal(undefined)).toBe("Not set");
    expect(shortPrincipal("")).toBe("Not set");
  });
});

describe("PrincipalName", () => {
  it("names every id of one render in a single request, with the full id as the title", async () => {
    const calls = nameService({ [ADA]: "Ada Admin", [BOB]: "Bob Builder" });
    renderWithApi(() => (
      <ul>
        <li>
          <PrincipalName id={ADA} />
        </li>
        <li>
          <PrincipalName id={BOB} />
        </li>
        <li>
          <PrincipalName id={GONE} />
        </li>
      </ul>
    ));
    expect(await screen.findByText("Ada Admin")).toHaveAttribute("title", ADA);
    expect(screen.getByText("Bob Builder")).toHaveAttribute("title", BOB);
    // Identity does not name this one: the short id stays, the full id is the tooltip.
    expect(screen.getByText("entra:9e9e9e9e")).toHaveAttribute("title", GONE);
    expect(calls).toEqual([[ADA, BOB, GONE]]);
  });

  it("shows the short id until the name arrives", async () => {
    let answer: () => void = () => undefined;
    const answered = new Promise<void>((resolve) => {
      answer = resolve;
    });
    server.use(
      http.post(NAMES, async () => {
        await answered;
        return HttpResponse.json({
          items: [{ id: ADA, display_name: "Ada Admin" }],
        });
      }),
    );
    renderWithApi(() => <PrincipalName id={ADA} />);
    expect(await screen.findByText("local:3f2a1b9c")).toHaveAttribute(
      "title",
      ADA,
    );
    answer();
    expect(await screen.findByText("Ada Admin")).toBeVisible();
    expect(screen.queryByText("local:3f2a1b9c")).toBeNull();
  });

  it("says Not set for a missing id and asks identity nothing", async () => {
    const calls = nameService({});
    renderWithApi(() => (
      <p data-testid="owner">
        <PrincipalName id={null} />
      </p>
    ));
    expect(await screen.findByTestId("owner")).toHaveTextContent("Not set");
    expect(calls).toEqual([]);
  });

  it("splits more than 100 ids into requests of at most 100", async () => {
    const ids = Array.from(
      { length: 101 },
      (_, index) => `local:${String(index).padStart(8, "0")}-rest`,
    );
    const calls = nameService({ [ids[100] ?? ""]: "The last one" });
    renderWithApi(() => (
      <>
        {ids.map((id) => (
          <PrincipalName key={id} id={id} />
        ))}
      </>
    ));
    expect(await screen.findByText("The last one")).toBeVisible();
    expect(calls.map((chunk) => chunk.length)).toEqual([100, 1]);
  });

  it("asks once for an id two separate caches want in the same tick", async () => {
    const calls = nameService({ [ADA]: "Ada Admin" });
    const other = new QueryClient();
    renderWithApi(() => (
      <>
        <PrincipalName id={ADA} />
        <QueryClientProvider client={other}>
          <PrincipalName id={ADA} />
        </QueryClientProvider>
      </>
    ));
    await waitFor(() => {
      expect(screen.getAllByText("Ada Admin")).toHaveLength(2);
    });
    expect(calls).toEqual([[ADA]]);
  });

  it("keeps the short id when the lookup fails or answers with no body", async () => {
    server.use(
      http.post(NAMES, () =>
        HttpResponse.json(
          { title: "Service temporarily unavailable" },
          { status: 503 },
        ),
      ),
    );
    renderWithApi(() => <PrincipalName id={ADA} />);
    expect(await screen.findByText("local:3f2a1b9c")).toBeVisible();

    server.use(http.post(NAMES, () => new HttpResponse(null, { status: 204 })));
    renderWithApi(() => <PrincipalName id={BOB} />);
    expect(await screen.findByText("local:7c1d2e3f")).toBeVisible();
  });
});

describe("usePrincipalLabel", () => {
  function ConflictNotice({ by }: { by: string | null }) {
    return <p>Changed by {usePrincipalLabel(by)}</p>;
  }

  it("is the name once known, else the short id or Not set", async () => {
    nameService({ [ADA]: "Ada Admin" });
    renderWithApi(() => (
      <>
        <ConflictNotice by={ADA} />
        <ConflictNotice by={GONE} />
        <ConflictNotice by={null} />
      </>
    ));
    expect(await screen.findByText("Changed by Ada Admin")).toBeVisible();
    expect(screen.getByText("Changed by entra:9e9e9e9e")).toBeVisible();
    expect(screen.getByText("Changed by Not set")).toBeVisible();
  });
});
