// Mounts a data hook (a query or mutation hook that reads the typed client from the router
// context) without the app shell, so its own branches can be driven through msw: a bodiless 2xx
// answer, the platform-route variant of a write, a failed request. `result.current` always holds
// the hook's latest return value. `renderWithApi` mounts a component the same way.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRouteWithContext,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { createApiClient, type ApiClient } from "@/api/client";
import { testConfig } from "@/test/renderApp";

export type HookResult<T> = { readonly current: T };

/** Renders `ui` inside a router context carrying the API client and a fresh query client. */
export function renderWithApi(ui: ReactNode): { queryClient: QueryClient } {
  const api: ApiClient = createApiClient(testConfig);
  const rootRoute = createRootRouteWithContext<{ api: ApiClient }>()({
    component: () => <>{ui}</>,
  });
  const router = createRouter({
    routeTree: rootRoute,
    context: { api },
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { queryClient };
}

/** Renders `useHook` inside the same context; resolves once it rendered. */
export async function renderDataHook<T>(
  useHook: () => T,
): Promise<{ result: HookResult<T>; queryClient: QueryClient }> {
  const box: { current?: T } = {};
  function Probe() {
    box.current = useHook();
    return <span data-testid="hook-probe" />;
  }
  const { queryClient } = renderWithApi(<Probe />);
  await screen.findByTestId("hook-probe");
  const result = {
    get current(): T {
      if (!("current" in box)) throw new Error("the hook has not rendered");
      return box.current as T;
    },
  };
  return { result, queryClient };
}
