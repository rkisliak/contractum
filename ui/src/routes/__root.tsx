import type { QueryClient } from "@tanstack/react-query";
import { createRootRouteWithContext, Outlet } from "@tanstack/react-router";
import type { ApiClient } from "@/api/client";
import type { Session } from "@/auth/session";
import { ToastProvider } from "@/components/Toast";
import type { RuntimeConfig } from "@/config";

export type RouterContext = {
  config: RuntimeConfig;
  queryClient: QueryClient;
  api: ApiClient;
  session: Session;
};

export const Route = createRootRouteWithContext<RouterContext>()({
  component: Root,
});

function Root() {
  return (
    <ToastProvider>
      <Outlet />
    </ToastProvider>
  );
}
