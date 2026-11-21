import { createRouter } from "@tanstack/react-router";
import { createElement } from "react";
import { NotFound } from "@/components/NotFound";
import type { RouterContext } from "@/routes/__root";
import { routeTree } from "./routeTree.gen";

export function createAppRouter(context: RouterContext) {
  return createRouter({
    routeTree,
    context,
    defaultNotFoundComponent: () =>
      createElement(NotFound, { unknownUrl: true }),
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
