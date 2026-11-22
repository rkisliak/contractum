// The UI's own build identity, injected by vite.config.ts (`define: __DC_BUILD__`) from the
// VERSION / GIT_SHA / BUILD_TIME image build arguments: the platform version of the release, the
// 12-character commit and the commit time. Under Vitest and `pnpm dev` nothing is injected, so it
// reads "dev".
declare const __DC_BUILD__:
  { version: string; commit: string; builtAt: string } | undefined;

export type BuildInfo = { version: string; commit: string; builtAt: string };

export const uiBuild: BuildInfo =
  typeof __DC_BUILD__ === "undefined"
    ? { version: "dev", commit: "dev", builtAt: "dev" }
    : __DC_BUILD__;
