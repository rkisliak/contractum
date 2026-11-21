// Every registered route renders the one page frame: a breadcrumb that starts at Home and an h1
// from PageHeader, inside the shell's main region. Unknown API calls answer 404, so detail routes
// exercise their Not found frame and list routes their empty or error state; either way the frame
// must be there.
import { QueryClient } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { createApiClient } from "@/api/client";
import { createSession } from "@/auth/session";
import { createAppRouter } from "@/router";
import { http, HttpResponse, server } from "@/test/msw";
import { renderApp, ROOT_ID, testConfig } from "@/test/renderApp";

const OTHER_ID = "11111111-2222-4333-8444-555555555555";

function leafAppPaths(): string[] {
  const api = createApiClient(testConfig);
  const router = createAppRouter({
    config: testConfig,
    queryClient: new QueryClient(),
    api,
    session: createSession(api, { navigate: () => undefined }),
  });
  const routes = Object.values(router.routesById) as {
    id: string;
    fullPath: string;
    children?: unknown;
  }[];
  return routes
    .filter((route) => route.id.startsWith("/_app/") && !route.children)
    .map(
      (route) =>
        route.fullPath
          .replace("$folderId", ROOT_ID)
          .replace(/\$[A-Za-z]+/g, OTHER_ID)
          .replace(/\/$/, "") || "/",
    )
    .sort();
}

const paths = leafAppPaths();

describe("the shared page frame", () => {
  it("covers every leaf route under the authenticated layout", () => {
    expect(paths.length).toBeGreaterThan(35);
  });

  it.each(paths)("%s renders a Home breadcrumb and an h1", async (path) => {
    server.use(
      http.all("*/api/v1/*", () =>
        HttpResponse.json(
          { type: "urn:dc:problem:not-found", title: "Not Found", status: 404 },
          {
            status: 404,
            headers: { "Content-Type": "application/problem+json" },
          },
        ),
      ),
    );
    renderApp(path);
    const main = await screen.findByRole("main");
    const heading = await within(main).findByRole(
      "heading",
      { level: 1 },
      { timeout: 4000 },
    );
    expect(heading).toHaveAttribute("data-page-heading");
    const crumbs = within(main).getByRole("navigation", {
      name: "Breadcrumb",
    });
    const items = within(crumbs).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("Home");
    expect(within(main).getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });
});
