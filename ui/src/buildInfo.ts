// The UI's build identity. The image holds the commit and the commit time, injected by
// vite.config.ts (`define: __DC_BUILD__`) from the GIT_SHA / BUILD_TIME build arguments, 12
// characters and an ISO time. The platform version is not part of the bundle: the nginx entrypoint
// writes it into config.json at startup (DC_UI_VERSION), so one build can be tested and then
// released unchanged. Under Vitest and `pnpm dev` nothing is injected, so the commit and time read
// "dev", and so does a config without a version.
import { useRouteContext } from "@tanstack/react-router";

declare const __DC_BUILD__: { commit: string; builtAt: string } | undefined;

export type BuildInfo = { version: string; commit: string; builtAt: string };

export const uiImageBuild: Pick<BuildInfo, "commit" | "builtAt"> =
  typeof __DC_BUILD__ === "undefined"
    ? { commit: "dev", builtAt: "dev" }
    : __DC_BUILD__;

/** The running UI: the platform version from the runtime config plus the image's own build. */
export function useUiBuild(): BuildInfo {
  const { config } = useRouteContext({ from: "__root__" });
  return { version: config.version ?? "dev", ...uiImageBuild };
}
