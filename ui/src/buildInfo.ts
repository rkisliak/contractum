// The UI's own build identity, injected by vite.config.ts (`define: __DC_BUILD__`) from
// package.json and the GIT_SHA / BUILD_TIME image build arguments. Under Vitest nothing is
// injected, so it reads "dev".
declare const __DC_BUILD__:
  { version: string; commit: string; builtAt: string } | undefined;

export type BuildInfo = { version: string; commit: string; builtAt: string };

export const uiBuild: BuildInfo =
  typeof __DC_BUILD__ === "undefined"
    ? { version: "dev", commit: "dev", builtAt: "dev" }
    : __DC_BUILD__;
